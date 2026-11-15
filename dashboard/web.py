"""LAN-only HTTP application; paired agents have individual ingestion tokens."""
import hashlib
import hmac
import json
import os
import sqlite3
from http.cookies import SimpleCookie, CookieError
from html import escape
from .access import AccessStore, COOKIE, SESSION_SECONDS
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, quote

from .render import render_dashboard
from .display import DEFAULT_WIDTH, DEFAULT_HEIGHT, Layout, TABS
from .codexbar import quota_view, history_view
from .panels import build_panels
from .http_guard import LocalRequestHandler, BoundedHTTPServer


class DashboardServer(BoundedHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, config_path, refresh=60, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
        self.store = store
        self.config_path = Path(config_path)
        self.access = AccessStore(self.config_path.parent/"access.sqlite3")
        self.refresh = refresh
        self.width, self.height = width,height
        super().__init__(address, Handler)

    def config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def summary(self):
        cfg=self.config()
        expected=[cfg["local_device"],*cfg.get("devices",[])]
        # Tokens and paths never leave the server configuration.
        expected=[{k:d[k] for k in ("id","name","platform")} for d in expected]
        data = self.store.summary(cfg.get("timezone_offset_minutes",480),expected)
        bridge = cfg.get("codexbar", {})
        data["quotas"] = quota_view(self.store.load_quota_state(), bridge.get("enabled", True) and getattr(self, "codexbar_enabled", True),
                                    interval=bridge.get("interval", 120))
        data["notice"] = "设备用量与账号额度分别统计；账号额度仅由汇总电脑读取一次。"
        data['token_history'] = history_view(self.store.load_token_history(), bridge.get('enabled',True) and getattr(self,'codexbar_enabled',True))
        return build_panels(self.store,data,cfg)


class Handler(LocalRequestHandler):
    def send_data(self, data, content_type, status=200, etag=None, headers=None):
        self.send_response(status)
        self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-cache" if etag else "no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("X-Frame-Options","DENY")
        self.send_header("Content-Security-Policy","frame-ancestors 'none'")
        if etag:
            self.send_header("ETag",etag)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if data:
            try:
                self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError):
                pass

    def send_json(self, value, status=200):
        self.send_data(json.dumps(value,ensure_ascii=False).encode(),"application/json; charset=utf-8",status)

    def viewer_token(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get('Cookie', ''))
            return cookie[COOKIE].value if COOKIE in cookie else None
        except CookieError:
            return None

    @staticmethod
    def safe_next(value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            return '/kindle'
        parsed = urlparse(value)
        if parsed.scheme or parsed.netloc or parsed.path not in ('/', '/kindle') or len(value) > 2048:
            return '/kindle'
        return quote(value, safe="/?=&%#;:+,@!$'()*-._~")

    def pairing_page(self, next_path='/kindle', message='', status=200):
        page = (Path(__file__).parent/'static'/'pair.html').read_text(encoding='utf-8')
        page = page.replace('<!--NEXT-->', escape(self.safe_next(next_path), quote=True))
        page = page.replace('<!--MESSAGE-->', '<p class="error" role="alert">'+escape(message)+'</p>' if message else '')
        self.send_data(page.encode(), 'text/html; charset=utf-8', status)

    def paired_redirect(self, next_path, token=None):
        href = escape(next_path, quote=True)
        # Include a usable HTML fallback for browsers that do not follow the redirect.
        page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <meta http-equiv="refresh" content="0;url={href}"><title>配对成功</title></head>
            <body style="margin:8%;font:18px sans-serif;color:#222;background:white">
            <h1 style="font-size:24px">配对成功</h1><a href="{href}">打开看板</a></body></html>'''
        headers = {'Location': next_path}
        if token is not None:
            headers['Set-Cookie'] = COOKIE+'='+token+'; Path=/; HttpOnly; SameSite=Strict; Max-Age='+str(SESSION_SECONDS)
        self.send_data(page.encode(), 'text/html; charset=utf-8', 303, headers=headers)

    def pair_viewer(self):
        try:
            origin = self.headers.get('Origin')
            if origin and origin not in ('http://'+self.headers.get('Host', ''), 'https://'+self.headers.get('Host', '')):
                return self.send_json({'error':'请求来源无效'}, 403)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096 or self.headers.get('Transfer-Encoding'):
                return self.send_json({'error':'配对请求无效'}, 400)
            if self.headers.get_content_type() != 'application/x-www-form-urlencoded':
                return self.send_json({'error':'配对请求格式无效'}, 400)
            form = parse_qs(self.rfile.read(length).decode('utf-8'), max_num_fields=8)
            next_path = self.safe_next(form.get('next', ['/kindle'])[0])
            # A saved session already grants access. Retried forms must not turn
            # an earlier success into an invalid-code error or create a second viewer.
            if self.server.access.authorize(self.viewer_token(), self.client_address[0]):
                return self.paired_redirect(next_path)
            try:
                token = self.server.access.pair(form.get('code', [''])[0], form.get('name', [''])[0],
                                                self.client_address[0], self.headers.get('User-Agent', ''))
            except ValueError as exc:
                return self.pairing_page(next_path, str(exc), 400)
            self.paired_redirect(next_path, token)
        except (ValueError, UnicodeError):
            self.send_json({'error':'配对请求无效'}, 400)
        except (OSError, sqlite3.Error):
            self.send_json({'error':'配对暂时不可用，请稍后重试'}, 503)

    def do_GET(self):
        parsed=urlparse(self.path)
        query=parse_qs(parsed.query)
        try:
            if parsed.path == '/access/status':
                if not self.server.access.authorize(self.viewer_token(), self.client_address[0]):
                    self.server.access.record_denied(self.client_address[0])
                    return self.send_json({'authorized': False}, 401)
                return self.send_json({'authorized': True})
            if parsed.path in ('/pair', '/access/pair'):
                next_path = self.safe_next(query.get('next', ['/kindle'])[0])
                if self.server.access.authorize(self.viewer_token(), self.client_address[0]):
                    return self.paired_redirect(next_path)
                return self.pairing_page(next_path)
            protected = parsed.path in ('/', '/index.html', '/api/summary', '/kindle', '/dashboard.png')
            if protected and not self.server.access.authorize(self.viewer_token(), self.client_address[0]):
                self.server.access.record_denied(self.client_address[0])
                if parsed.path in ('/', '/index.html', '/kindle'):
                    return self.pairing_page('/' if parsed.path == '/index.html' else self.path, status=401)
                return self.send_json({'error':'请先在浏览器中配对设备'}, 401)
            if parsed.path in ("/","/index.html"):
                self.send_data((Path(__file__).parent/"static"/"index.html").read_bytes(),"text/html; charset=utf-8")
            elif parsed.path=="/health":
                instance=hashlib.sha256(str(self.server.config_path.resolve()).encode()).hexdigest()[:16]
                self.send_json({"ok":True,"service":"kindle-dashboard","instance":instance,"pid":os.getpid()})
            elif parsed.path=="/api/summary":
                self.send_json(self.server.summary())
            elif parsed.path in ("/kindle","/dashboard.png"):
                width=int(query.get("width",[self.server.width])[0])
                height=int(query.get("height",[self.server.height])[0])
                if not 400<=width<=1600 or not 400<=height<=2200:
                    raise ValueError("画布范围：宽 400–1600，高 400–2200")
                view=query.get("view",["usage"])[0]
                if view not in {key for label,key in TABS}|{'devices','quota'}:
                    raise ValueError("未知页面")
                params={"width":width,"height":height,"view":view}
                snapshot=self.server.summary()
                device=query.get('device',[None])[0]
                if device:
                    if device not in {d['id'] for d in snapshot['devices']}:raise ValueError('未知设备')
                    params['device']=device
                if parsed.path=="/dashboard.png":
                    buf=BytesIO()
                    render_dashboard(width,height,snapshot,view,device).save(buf,format="PNG")
                    data=buf.getvalue()
                    etag='"'+hashlib.sha256(data).hexdigest()+'"'
                    if self.headers.get("If-None-Match")==etag:
                        self.send_data(b"","image/png",304,etag)
                    else:
                        self.send_data(data,"image/png",etag=etag)
                else:
                    tabs=[]
                    # Share tab definitions with the renderer so hit areas cannot drift.
                    layout = Layout(width,height)
                    for i,(label,key) in enumerate(TABS):
                        href="/kindle?"+urlencode({**params,"view":key})
                        x,y,tw,th = layout.tab_percent(i)
                        tabs.append(f'<a aria-label="{label}" href="{href}" style="left:{x}%;top:{y}%;width:{tw}%;height:{th}%"></a>')
                    links=[('设备','devices',device,layout.device_button())]
                    if view=='devices':
                        links += [(d['name']+' Coding Plan','usage',d['id'],layout.device_row(i))
                            for i,d in enumerate(snapshot['devices']) if layout.device_row(i)[1]+132<=layout.logical_height-46]
                    from html import escape
                    for label,key,target,rect in links:
                        href='/kindle?'+urlencode({**params,'view':key,**({'device':target} if target else {})})
                        x,y,tw,th=layout.percent(rect)
                        tabs.append(f'<a aria-label="{escape(label,quote=True)}" href="{href}" style="left:{x}%;top:{y}%;width:{tw}%;height:{th}%"></a>')
                    html=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
                        <meta name="viewport" content="width=device-width, initial-scale=1">
                        <noscript><meta http-equiv="refresh" content="{self.server.refresh}"></noscript>
                        <title>AI USAGE · Kindle</title><style>
                        html,body{{margin:0;width:100%;height:100%;background:white}}
                        main{{position:relative;max-width:{width}px;margin:0 auto}}
                        img{{display:block;width:100%;height:auto}}a{{position:absolute;display:block}}
                        a:focus{{outline:3px solid black}}
                        #connection{{position:absolute;bottom:0;left:0;right:0;padding:8px 4px;
                        border-top:2px solid black;background:white;color:black;font:16px sans-serif;text-align:center}}
                        #connection[hidden]{{display:none}}</style></head><body>
                        <main id="screen"><img id="picture" alt="AI 双端用量看板" width="{width}" height="{height}" src="/dashboard.png?{urlencode(params)}">{''.join(tabs)}
                        <div id="connection" role="status" hidden>连接中断 · 自动重试</div></main>
                        <script>(function(){{
                            function fit(){{
                                var root=document.documentElement;
                                var vw=root.clientWidth||window.innerWidth;
                                var vh=root.clientHeight||window.innerHeight;
                                if(!vw||!vh) return;
                                var scale=Math.min(vw/{width},vh/{height},1);
                                var frame=document.getElementById('screen');
                                frame.style.width=Math.floor({width}*scale)+'px';
                                frame.style.height=Math.floor({height}*scale)+'px';
                            }}
                            window.onresize=fit;window.onload=fit;fit();
                            // ES5 for older Kindle browsers. Keep the last successful
                            // picture while the next frame loads or the network recovers.
                            var picture=document.getElementById('picture');
                            var connection=document.getElementById('connection');
                            var interval={self.server.refresh}*1000;
                            var retry=Math.min(interval,15000);
                            var timer=null,inFlight=false;
                            function checkAccess(){{
                                try{{
                                    var request=new XMLHttpRequest();
                                    request.open('GET','/access/status',true);
                                    request.timeout=5000;
                                    request.onreadystatechange=function(){{
                                        if(request.readyState===4&&request.status===401)
                                            window.location.replace('/pair?next='+encodeURIComponent(window.location.pathname+window.location.search));
                                    }};
                                    request.send();
                                }}catch(e){{}}
                            }}
                            function later(delay){{
                                window.clearTimeout(timer);
                                timer=window.setTimeout(update,delay);
                            }}
                            function update(){{
                                if(inFlight) return;
                                inFlight=true;
                                var next=new Image(),done=false;
                                var watchdog=window.setTimeout(function(){{finish(false);}},10000);
                                function finish(ok){{
                                    if(done) return;
                                    done=true;inFlight=false;
                                    window.clearTimeout(watchdog);
                                    next.onload=null;next.onerror=null;
                                    if(ok) picture.src=next.src;
                                    connection.hidden=ok;
                                    if(!ok) checkAccess();
                                    later(ok?interval:retry);
                                }}
                                next.onload=function(){{finish(true);}};
                                next.onerror=function(){{finish(false);}};
                                next.src='/dashboard.png?{urlencode(params)}&_='+new Date().getTime();
                            }}
                            picture.onerror=function(){{connection.hidden=false;checkAccess();later(retry);}};
                            picture.onload=function(){{if(!inFlight) connection.hidden=true;}};
                            if(picture.complete&&!picture.naturalWidth){{connection.hidden=false;later(retry);}}
                            else later(interval);
                            if(window.addEventListener) window.addEventListener('online',update,false);
                        }})();</script>
                        </body></html>'''
                    self.send_data(html.encode(),"text/html; charset=utf-8")
            else:
                self.send_json({"error":"页面不存在"},404)
        except (ValueError,KeyError,TypeError) as exc:
            self.send_json({"error":str(exc)},400)
        except (OSError, sqlite3.Error):
            self.send_json({"error":"暂时无法读取看板，请稍后重试"},503)

    def do_POST(self):
        if urlparse(self.path).path == '/access/pair':
            return self.pair_viewer()
        if urlparse(self.path).path!="/api/ingest":
            return self.send_json({"error":"接口不存在"},404)
        try:
            self.connection.settimeout(15)
            length=int(self.headers.get("Content-Length","0"))
            if not 0<length<=1024*1024:
                return self.send_json({"error":"请求过大或为空"},413)
            auth=self.headers.get("Authorization","")
            cfg=self.server.config()
            device=next((d for d in cfg.get("devices",[]) if
                         hmac.compare_digest(auth,"Bearer "+d["token"])),None)
            if device is None:
                return self.send_json({"error":"配对凭据无效"},401)
            body=self.rfile.read(length)
            payload=json.loads(body)
            if payload["device"]["id"]!=device["id"]:
                return self.send_json({"error":"设备身份不匹配"},403)
            inserted=self.server.store.ingest(device,payload["events"],payload.get("status",{}))
            self.send_json({"accepted":len(payload["events"]),"inserted":inserted})
        except (ValueError,KeyError,TypeError,UnicodeError,AttributeError,OverflowError,RecursionError):
            self.send_json({"error":"上报格式不正确；整批未接受"},400)
        except (OSError,TimeoutError,sqlite3.Error):
            self.send_json({"error":"上报超时，请稍后重试"},503)

    def log_message(self, *args):
        pass
