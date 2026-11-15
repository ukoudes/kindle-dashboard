"""Read-only viewer and pairing relay to a configured dashboard host."""
import ipaddress
import os
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from .http_guard import LocalRequestHandler, BoundedHTTPServer, NoRedirect
from http.cookies import SimpleCookie, CookieError
from .access import COOKIE


def server_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Mac 地址必须是 http(s)://主机:端口')
    return value.rstrip('/')


def hotspot_addresses():
    """Best effort discovery of Wi-Fi Direct virtual interfaces, never guess an IP."""
    if os.name != 'nt':
        return []
    script = ("Get-NetAdapter -IncludeHidden | Where-Object {$_.Status -eq 'Up' -and $_.InterfaceDescription -like '*Wi-Fi Direct*'} | "
              "ForEach-Object {Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue} | "
              "Select-Object -ExpandProperty IPAddress")
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, text=True, timeout=8)
        addresses = []
        for line in result.stdout.splitlines():
            try:
                address = ipaddress.IPv4Address(line.strip())
            except ipaddress.AddressValueError:
                continue
            if not address.is_loopback and not address.is_link_local and not address.is_unspecified:
                addresses.append(str(address))
        return sorted(set(addresses))
    except (OSError, subprocess.TimeoutExpired):
        return []


class RelayServer(BoundedHTTPServer):
    daemon_threads = True

    def __init__(self, address, get_config, timeout=8):
        self.get_config = get_config
        self.timeout = timeout
        # LAN requests must not accidentally go through a system HTTP proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        server_url(get_config()['server_url'])
        super().__init__(address, RelayHandler)


class RelayHandler(LocalRequestHandler):
    ALLOWED = {'/', '/index.html', '/kindle', '/dashboard.png', '/api/summary', '/health', '/pair', '/access/status'}
    MAX_RESPONSE = 8 * 1024 * 1024

    def reply(self, status, body, content_type='text/html; charset=utf-8', etag=None, headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache' if etag else 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        if etag:
            self.send_header('ETag', etag)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def unavailable(self):
        body = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <meta http-equiv="refresh" content="15"><title>连接状态</title></head>
        <body style="margin:8%;font:24px sans-serif;color:black;background:white">
        <h2>Windows 已连接</h2><p>暂时无法访问 Mac</p>
        <p>检查 Mac 看板是否运行，以及 Windows 能否打开 Mac 的看板地址。</p>
        <p>正在自动重试…</p></body></html>'''.encode()
        self.reply(502, body)

    def do_GET(self):
        return self.forward()

    def forward(self, body=None):
        path = urlsplit(self.path)
        allowed = self.ALLOWED if body is None else {'/access/pair'}
        if path.scheme or path.netloc or path.path not in allowed or len(self.path) > 2048:
            return self.reply(404, b'Not found', 'text/plain')
        try:
            base = server_url(self.server.get_config()['server_url'])
            target = '/kindle' if path.path in ('/', '/index.html') else path.path
            if path.query:
                target += '?' + path.query
            headers = {}
            etag = self.headers.get('If-None-Match')
            if etag:
                headers['If-None-Match'] = etag
            # Forward only the viewer cookie, never ingestion credentials or other cookies.
            try:
                cookies = SimpleCookie()
                cookies.load(self.headers.get('Cookie', ''))
                if COOKIE in cookies:
                    headers['Cookie'] = COOKIE+'='+cookies[COOKIE].value
            except CookieError:
                pass
            if body is not None:
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
            request = urllib.request.Request(base + target, data=body, headers=headers)
            try:
                response = self.server.opener.open(request, timeout=self.server.timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                if response.code not in (200, 303, 304, 400, 401, 403, 404, 429):
                    return self.unavailable()
                content_type = response.headers.get('Content-Type', '')
                if not any(content_type.startswith(t) for t in ('text/html', 'image/png', 'application/json', 'text/plain')):
                    return self.unavailable()
                body = response.read(self.MAX_RESPONSE + 1)
                if len(body) > self.MAX_RESPONSE:
                    return self.unavailable()
                extra = {}
                if response.code == 303:
                    location = response.headers.get('Location', '')
                    if not location.startswith('/') or location.startswith('//'):
                        return self.unavailable()
                    extra['Location'] = location
                    cookie = response.headers.get('Set-Cookie', '')
                    if cookie.startswith(COOKIE+'='):
                        extra['Set-Cookie'] = cookie
                self.reply(response.code, body, content_type, response.headers.get('ETag'), extra)
        except (OSError, ValueError, KeyError, TypeError):
            self.unavailable()

    def do_POST(self):
        if urlsplit(self.path).path != '/access/pair':
            return self.reply(405, b'Read only', 'text/plain')
        try:
            origin = self.headers.get('Origin')
            if origin and origin not in ('http://'+self.headers.get('Host', ''), 'https://'+self.headers.get('Host', '')):
                return self.reply(403, b'Invalid origin', 'text/plain')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096 or self.headers.get('Transfer-Encoding'):
                return self.reply(400, b'Invalid request', 'text/plain')
            if self.headers.get_content_type() != 'application/x-www-form-urlencoded':
                return self.reply(400, b'Invalid content type', 'text/plain')
            return self.forward(self.rfile.read(length))
        except (ValueError, OSError):
            return self.reply(400, b'Invalid request', 'text/plain')

    def log_message(self, *args):
        pass
