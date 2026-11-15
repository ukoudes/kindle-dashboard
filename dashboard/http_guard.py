"""Small shared request boundary for the LAN dashboard and read-only relay."""
import ipaddress
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._connections = BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


class LocalRequestHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def parse_request(self):
        if not super().parse_request():
            return False
        try:
            hosts = self.headers.get_all('Host', [])
            if len(hosts) != 1:
                raise ValueError('missing or ambiguous host')
            target = urlsplit('//'+hosts[0])
            host = target.hostname
            if not host or target.username or target.password or target.path or target.query or target.fragment:
                raise ValueError('invalid host')
            # Validate port syntax even though routing never uses this value.
            target.port
            local = socket.gethostname().lower().rstrip('.')
            if host.lower().rstrip('.') not in {'localhost', local, local.removesuffix('.local')+'.local'}:
                ipaddress.ip_address(host)
            # Reject DNS rebinding hostnames and browser requests from other sites.
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise ValueError('cross-site request')
        except ValueError:
            self.send_error(403, 'Use the local dashboard address')
            return False
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) > 1 or self.headers.get('Transfer-Encoding'):
            self.send_error(400, 'Invalid request framing')
            return False
        return True
