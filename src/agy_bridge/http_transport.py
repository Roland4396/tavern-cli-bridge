"""HTTP framing only; never selects an account or starts inference."""
import hmac
import json
import select
import socket
from http.server import BaseHTTPRequestHandler
from .core import BridgeError

class TransportHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def setup(self): super().setup(); self.connection.settimeout(None)
    def log_message(self, *args): pass
    def application(self): return self.server.app
    def authorized(self): return hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token)
    def read_json(self):
        """Read exactly one bounded body; reject ambiguous HTTP framing."""
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
            raise BridgeError(400, 'invalid_request', 'One Content-Length is required; Transfer-Encoding is not supported')
        try:
            length = int(lengths[0])
        except ValueError:
            raise BridgeError(400, 'invalid_request', 'Invalid Content-Length') from None
        if not 0 < length <= 10000000:
            raise BridgeError(413, 'input_too_large', 'Invalid request body length')
        try:
            raw = self.rfile.read(length)
        except OSError:
            raise BridgeError(400, 'request_read_error', 'Request body could not be read') from None
        if len(raw) != length:
            raise BridgeError(400, 'invalid_request', 'Truncated request body')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise BridgeError(400, 'invalid_request', 'Invalid JSON or request encoding') from None
        if not isinstance(value, dict):
            raise BridgeError(400, 'invalid_request', 'Expected a JSON object')
        return value
    def reply(self, status, obj, extra=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.send_header('Content-Length', str(len(data))); self.send_header('Connection', 'close')
        for k, v in (extra or {}).items(): self.send_header(k, str(v))
        self.end_headers(); self.wfile.write(data); self.wfile.flush(); self.close_connection = True
    def gone(self):
        try:
            if select.select([self.connection], [], [], 0)[0]: return not self.connection.recv(1, socket.MSG_PEEK)
        except (OSError, ValueError): return True
        return False
