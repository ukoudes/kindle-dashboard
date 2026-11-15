"""Transport regressions: redirects must not leak pairing credentials or lose queued usage."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dashboard.collector import Collector, run_agent
from dashboard.storage import utcnow


class AgentTransportTests(unittest.TestCase):
    def test_redirect_never_forwards_credentials_and_keeps_pending_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seen = []
            class Destination(BaseHTTPRequestHandler):
                def do_GET(self):
                    seen.append('Authorization' in self.headers)
                    body = b'{"accepted":1}'
                    self.send_response(200)
                    self.send_header('Content-Length',str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self,*args): pass
            destination = ThreadingHTTPServer(('127.0.0.1',0),Destination)
            class Redirect(BaseHTTPRequestHandler):
                def do_POST(self):
                    self.rfile.read(int(self.headers['Content-Length']))
                    self.send_response(302)
                    self.send_header('Location','http://127.0.0.1:'+str(destination.server_port)+'/other')
                    self.send_header('Content-Length','0')
                    self.end_headers()
                def log_message(self,*args): pass
            redirect = ThreadingHTTPServer(('127.0.0.1',0),Redirect)
            for server in (destination, redirect):
                threading.Thread(target=server.serve_forever,daemon=True).start()
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
            event = {'id':'one','source':'test','model':'test','timestamp':utcnow(),
                     'input_tokens':1,'output_tokens':1}
            (root/'events.jsonl').write_text(json.dumps(event)+'\n')
            config = {'id':'test','name':'Test','platform':'Windows','token':'test-only-token',
                'jsonl_paths':[str(root/'events.jsonl')],
                'server_url':'http://127.0.0.1:'+str(redirect.server_port)}
            succeeded = run_agent(config,root/'agent.sqlite3',once=True)
            self.assertFalse(any(seen), 'Authorization must never reach a redirect target')
            self.assertEqual(seen, [], 'Redirect target must not be contacted')
            self.assertFalse(succeeded)
            collector = Collector(root/'agent.sqlite3',config)
            try: self.assertEqual(len(collector.batch()),1)
            finally: collector.close()


if __name__ == '__main__': unittest.main()
