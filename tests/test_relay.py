import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

from PIL import Image

from dashboard.relay import RelayServer, server_url
from dashboard.storage import Store, utcnow
from dashboard.web import DashboardServer
from kindle_dashboard import write_json


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        config=root/'hub.json'
        device={'id':'mac','name':'MacBook','platform':'Darwin'}
        write_json(config,{'local_device':device,'devices':[]})
        store=Store(root/'hub.sqlite3')
        store.ingest(device,[{'id':'one','source':'Codex','model':'test-model','timestamp':utcnow(),
                            'input_tokens':100,'output_tokens':10}],{})
        hub=DashboardServer(('127.0.0.1',0),store,config)
        upstream=self.start(hub)
        self.access=hub.access
        code=hub.access.create_pairing()['pairing']['code']
        self.viewer_token=hub.access.pair(code,'Relay browser','127.0.0.1','test')
        self.client={'server_url':self.url(upstream),'token':'PRIVATE-PAIRING-TOKEN'}
        self.relay=self.start(RelayServer(('127.0.0.1',0),lambda:self.client,timeout=1))
        self.base=self.url(self.relay)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.opener.addheaders.append(('Cookie','kindle_access='+self.viewer_token))

    def start(self,server):
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def url(self,server):
        return f'http://127.0.0.1:{server.server_port}'

    def get(self,path):
        return self.opener.open(self.base+path,timeout=3)

    def test_browser_html_uses_relay_relative_links(self):
        for path in ('/','/kindle','/kindle?view=quota','/kindle?view=devices'):
            with self.get(path) as response:
                body=response.read().decode()
                self.assertEqual(response.status,200)
                self.assertIn("src=\"/dashboard.png?",body)
                self.assertNotIn(self.client['server_url'],body)
                self.assertNotIn(self.client['token'],body)

    def test_relay_also_rejects_rebinding_and_cross_site_reads(self):
        for headers in ({'Host':'attacker.example'},{'Sec-Fetch-Site':'cross-site'}):
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as exc:
                self.opener.open(urllib.request.Request(self.base+'/api/summary',headers=headers))
            self.assertEqual(exc.exception.code,403)

    def test_real_png_and_shared_summary_across_two_hops(self):
        with self.get('/dashboard.png') as response:
            image=Image.open(BytesIO(response.read()))
            self.assertEqual(image.size,(1072,1448))
        with self.get('/api/summary') as response:
            summary=json.load(response)
            self.assertEqual(summary['totals']['tokens'],110)

    def test_relay_preserves_conditional_png_requests(self):
        with self.get('/dashboard.png') as response:
            etag=response.headers['ETag']
        request=urllib.request.Request(self.base+'/dashboard.png',headers={'If-None-Match':etag})
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.opener.open(request,timeout=3)
        self.assertEqual(exc.exception.code,304)

    def test_upstream_failure_returns_readable_retry_page(self):
        self.client['server_url']='http://127.0.0.1:1'
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.get('/kindle')
        self.assertEqual(exc.exception.code,502)
        body=exc.exception.read().decode()
        self.assertIn('Windows 已连接',body)
        self.assertIn('暂时无法访问 Mac',body)
        self.assertNotIn('PRIVATE-PAIRING-TOKEN',body)

    def test_routes_are_read_only_and_never_expose_local_files(self):
        for path in ('/api/ingest','/clients/agent.json','/data/hub.json','/../clients/agent.json'):
            with self.assertRaises(urllib.error.HTTPError) as exc:
                self.get(path)
            self.assertEqual(exc.exception.code,404)
        req=urllib.request.Request(self.base+'/api/ingest',data=b'{}')
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.opener.open(req,timeout=3)
        self.assertEqual(exc.exception.code,405)

    def test_rejects_credential_urls_and_non_origin_targets(self):
        for value in ('file:///tmp/file','http://user:secret@host','http://host/path','http://host?token=secret'):
            with self.assertRaises(ValueError):server_url(value)

    def test_never_forwards_browser_auth_cookie_or_follows_redirects(self):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                self.send_response(302)
                self.send_header('Location','http://127.0.0.1:1/secret')
                self.send_header('Content-Length','0')
                self.end_headers()
            def log_message(self,*args):pass
        upstream=self.start(ThreadingHTTPServer(('127.0.0.1',0),Handler))
        self.client['server_url']=self.url(upstream)
        req=urllib.request.Request(self.base+'/kindle',headers={'Authorization':'Bearer PRIVATE','Cookie':'secret=PRIVATE'})
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.opener.open(req,timeout=3)
        self.assertEqual(exc.exception.code,502)
        self.assertEqual(len(seen),1)
        self.assertNotIn('Authorization',seen[0])
        self.assertNotIn('Cookie',seen[0])


if __name__=='__main__':
    unittest.main()
