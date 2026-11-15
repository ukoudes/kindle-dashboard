import http.cookiejar
import http.client
import json
from pathlib import Path
import sqlite3
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

from dashboard.access import AccessStore, COOKIE
from dashboard.storage import Store
from dashboard.web import DashboardServer
from dashboard.relay import RelayServer
from kindle_dashboard import write_json


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.access = AccessStore(self.root/'access.sqlite3')

    def pair(self, name='我的 Kindle'):
        code = self.access.create_pairing()['pairing']['code']
        return self.access.pair(code, name, '192.0.2.10', 'Kindle/1.0')

    def test_session_persists_hashes_only_and_revokes(self):
        token = self.pair()
        self.assertTrue(AccessStore(self.access.path).authorize(token, '192.0.2.10'))
        state = self.access.state()
        self.assertNotIn(token, json.dumps(state))
        self.assertEqual(state['devices'][0]['browser'], 'Kindle')
        with sqlite3.connect(self.access.path) as db:
            self.assertNotIn(token, str(db.execute('SELECT * FROM viewers').fetchall()))
        self.access.manage('revoke', state['devices'][0]['id'])
        self.assertFalse(self.access.authorize(token, '192.0.2.10'))

    def test_expired_code_and_session_and_reset(self):
        code = self.access.create_pairing()['pairing']['code']
        with patch('dashboard.access.time.time', return_value=time.time()+301):
            with self.assertRaises(ValueError): self.access.pair(code, 'Kindle', '127.0.0.1', '')
        token = self.pair()
        with patch('dashboard.access.time.time', return_value=time.time()+91*86400):
            self.assertFalse(self.access.authorize(token, '192.0.2.10'))
        other = self.pair('电脑')
        self.access.manage('reset')
        self.assertFalse(self.access.authorize(other, '192.0.2.10'))
        self.assertEqual(self.access.state()['devices'], [])

    def test_one_use_even_with_concurrent_requests(self):
        code = self.access.create_pairing()['pairing']['code']
        def attempt(i):
            try: return self.access.pair(code, str(i), '192.0.2.10', '')
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(attempt, range(12)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(len(self.access.state()['devices']), 1)

    def test_code_attempts_and_bounded_devices_and_denied_sources(self):
        code = self.access.create_pairing()['pairing']['code']
        for _ in range(10):
            with self.assertRaises(ValueError): self.access.pair('wrong', 'Kindle', '192.0.2.10', '')
        with self.assertRaises(ValueError): self.access.pair(code, 'Kindle', '192.0.2.10', '')
        for i in range(8): self.pair(str(i))
        with self.assertRaises(ValueError): self.access.create_pairing()
        for i in range(50): self.access.record_denied('192.0.2.'+str(i))
        self.assertEqual(len(self.access.state()['denied']), 20)
        with patch('dashboard.access.time.time', return_value=time.time()+86401):
            self.assertEqual(self.access.state()['denied'], [])

    def start(self, server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:'+str(server.server_port)

    def http_fixture(self):
        config = self.root/'hub.json'
        write_json(config, {'local_device':{'id':'mac','name':'PRIVATE DEVICE','platform':'Darwin'},'devices':[]})
        self.server = DashboardServer(('127.0.0.1',0),Store(self.root/'hub.sqlite3'),config)
        base = self.start(self.server)
        self.browser = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        return base

    def submit(self, base, code, name='我的 Kindle', **fields):
        body = urllib.parse.urlencode({'name':name,'code':code, **fields}).encode()
        return self.browser.open(urllib.request.Request(base+'/access/pair',body,
                    headers={'Content-Type':'application/x-www-form-urlencoded'}),timeout=3)

    def assert_locked(self, base):
        for path in ('/','/kindle','/kindle?view=devices','/api/summary','/dashboard.png','/access/status'):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as exc:
                self.browser.open(base+path,timeout=3)
            self.assertEqual(exc.exception.code,401)
            self.assertNotIn(b'PRIVATE DEVICE',exc.exception.read())

    def test_all_read_routes_fail_closed_then_pair_and_revoke(self):
        base = self.http_fixture()
        self.assert_locked(base)
        with self.browser.open(base+'/health') as response:
            self.assertTrue(json.load(response)['ok'])
        code = self.server.access.create_pairing()['pairing']['code']
        with self.submit(base, code, next='/kindle?view=api') as response:
            self.assertTrue(response.url.endswith('/kindle?view=api'))
            self.assertIn(b'dashboard.png',response.read())
        with self.browser.open(base+'/api/summary') as response:
            self.assertIn(b'PRIVATE DEVICE',response.read())
        self.server.access.manage('revoke',self.server.access.state()['devices'][0]['id'])
        self.assert_locked(base)
        with self.assertRaises(urllib.error.HTTPError) as exc: self.submit(base,code)
        self.assertEqual(exc.exception.code,400)

    def test_cross_origin_pairing_is_rejected_and_code_not_exposed(self):
        base = self.http_fixture()
        code = self.server.access.create_pairing()['pairing']['code']
        with self.browser.open(base+'/pair') as response:
            self.assertNotIn(code.encode(),response.read())
        req = urllib.request.Request(base+'/access/pair',urllib.parse.urlencode({'name':'Bad','code':code}).encode(),
                headers={'Origin':'http://evil.example','Content-Type':'application/x-www-form-urlencoded'})
        with self.assertRaises(urllib.error.HTTPError) as exc: self.browser.open(req)
        self.assertEqual(exc.exception.code,403)
        self.assertEqual(self.server.access.state()['devices'],[])
        self.assertEqual(self.server.RequestHandlerClass.safe_next('/kindle?x=1\r\nX-Test: yes'),'/kindle')
        with self.submit(base,code,next='//evil.example') as response:
            self.assertTrue(response.url.startswith(base+'/kindle'))

    def test_repeated_pairing_submission_with_saved_session_is_not_an_error(self):
        base = self.http_fixture()
        code = self.server.access.create_pairing()['pairing']['code']
        with self.submit(base, code) as response:
            self.assertEqual(response.status, 200)
        new_code = self.server.access.create_pairing()['pairing']['code']
        # A repeated form submission must not report failure after pairing succeeded.
        with self.submit(base, code) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b'dashboard.png', response.read())
        self.assertEqual(len(self.server.access.state()['devices']), 1)
        self.assertEqual(self.server.access.state()['pairing']['code'], new_code)

    def test_refreshing_pair_endpoint_and_unicode_return_address(self):
        base=self.http_fixture()
        with self.browser.open(base+'/access/pair') as response:
            self.assertEqual(response.status,200)
            self.assertIn(b'<form',response.read())
        code=self.server.access.create_pairing()['pairing']['code']
        with self.submit(base,code,next='/kindle?label=中文') as response:
            self.assertEqual(response.status,200)
            self.assertIn('%E4%B8%AD%E6%96%87',response.url)
        with self.browser.open(base+'/access/pair') as response:
            self.assertIn(b'dashboard.png',response.read())

    def test_ambiguous_request_lengths_cannot_create_authorization(self):
        self.http_fixture()
        code=self.server.access.create_pairing()['pairing']['code']
        body=urllib.parse.urlencode({'code':code,'name':'Test'}).encode()
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=2)
        self.addCleanup(connection.close)
        connection.putrequest('POST','/access/pair')
        connection.putheader('Content-Type','application/x-www-form-urlencoded')
        connection.putheader('Content-Length',str(len(body)))
        connection.putheader('Content-Length',str(len(body)+1))
        connection.endheaders(body)
        response=connection.getresponse()
        self.assertEqual(response.status,400)
        response.read()
        self.assertEqual(self.server.access.state()['devices'],[])
        self.assertEqual(self.server.access.state()['pairing']['code'],code)

    def test_pair_redirect_has_html_fallback_without_exposing_code(self):
        from dashboard.relay import NoRedirect
        base = self.http_fixture()
        code = self.server.access.create_pairing()['pairing']['code']
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(base+'/access/pair',
            urllib.parse.urlencode({'name':'Kindle','code':code,'next':'/kindle?view=api'}).encode(),
            headers={'Content-Type':'application/x-www-form-urlencoded'})
        with self.assertRaises(urllib.error.HTTPError) as exc:
            opener.open(request,timeout=3)
        response = exc.exception
        self.assertEqual(response.code,303)
        self.assertEqual(response.headers.get_content_type(),'text/html')
        self.assertEqual(response.headers['Location'],'/kindle?view=api')
        self.assertIn('HttpOnly',response.headers['Set-Cookie'])
        body=response.read()
        self.assertIn(b'href="/kindle?view=api"',body)
        self.assertNotIn(code.encode(),body)

    def test_connection_limit_releases_slots_after_clients_close(self):
        from dashboard.http_guard import BoundedHTTPServer, LocalRequestHandler
        ready = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        count = [0]
        class HoldingHandler(LocalRequestHandler):
            def do_GET(self):
                with lock:
                    count[0] += 1
                    if count[0] == 16: ready.set()
                release.wait(3)
                self.send_response(200)
                self.send_header('Content-Length','0')
                self.end_headers()
            def log_message(self, *args): pass
        server = BoundedHTTPServer(('127.0.0.1',0),HoldingHandler)
        self.start(server)
        self.addCleanup(release.set)
        clients = []
        try:
            for _ in range(16):
                client = socket.create_connection(server.server_address,timeout=2)
                clients.append(client)
                client.sendall(b'GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n')
            self.assertTrue(ready.wait(2))
            with socket.create_connection(server.server_address,timeout=2) as extra:
                extra.sendall(b'GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n')
                try: self.assertEqual(extra.recv(1),b'')
                except ConnectionResetError: pass
            release.set()
            for client in clients:
                self.assertIn(b'200',client.recv(1024))
            with urllib.request.urlopen('http://127.0.0.1:'+str(server.server_port)+'/health',timeout=2) as response:
                self.assertEqual(response.status,200)
        finally:
            release.set()
            for client in clients: client.close()

    def test_relay_pairing_cookie_revocation_and_no_upload_privilege(self):
        self.http_fixture()
        relay = self.start(RelayServer(('127.0.0.1',0),lambda:{'server_url':
                'http://127.0.0.1:'+str(self.server.server_port)},timeout=2))
        self.assert_locked(relay)
        code = self.server.access.create_pairing()['pairing']['code']
        with self.submit(relay,code) as response:
            self.assertIn(b'dashboard.png',response.read())
        with self.submit(relay,code) as response:
            self.assertIn(b'dashboard.png',response.read())
        self.assertEqual(len(self.server.access.state()['devices']),1)
        with self.browser.open(relay+'/api/summary') as response:
            self.assertIn(b'PRIVATE DEVICE',response.read())
        self.server.access.manage('reset')
        self.assert_locked(relay)
        request=urllib.request.Request(relay+'/api/ingest',b'{}')
        with self.assertRaises(urllib.error.HTTPError) as exc: self.browser.open(request)
        self.assertEqual(exc.exception.code,405)


if __name__ == '__main__': unittest.main()
