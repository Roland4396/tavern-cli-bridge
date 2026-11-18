"""Ephemeral schema-only registry. No tool implementations exist here.
Tool dispatch attempts are held without executing or returning any tool result.
The API captures the completed model response and cancels the CLI at handoff.
"""
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .protocol import field

class SchemaRegistry:
    def __init__(self, tools):
        self.tools = tools; self.call_attempts = 0; self.metadata_requests = 0
        self.cancelled = threading.Event(); self.executions = 0
        self.path = '/schema/' + secrets.token_hex(24)
        registry = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, code, value=None):
                data = b'' if value is None else json.dumps(value, ensure_ascii=False).encode()
                self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            def do_GET(self): self.reply(405)
            def do_DELETE(self): self.reply(200)
            def do_POST(self):
                if self.path != registry.path: self.reply(404); return
                try:
                    n = int(self.headers.get('Content-Length', '0'))
                    if not 0 < n < 1048576: self.reply(413); return
                    req = json.loads(self.rfile.read(n))
                except Exception: self.reply(400); return
                method = req.get('method'); rid = req.get('id')
                if method == 'tools/call':
                    registry.call_attempts += 1
                    registry.cancelled.wait(600)
                    # No implementation, subprocess, network target or caller code exists.
                    # Never fabricate a successful tool result. Release only on cleanup.
                    try: self.reply(200, {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'Execution belongs to the API caller'}})
                    except OSError: pass
                    return
                registry.metadata_requests += 1
                if rid is None: self.reply(202); return
                result = {}
                if method == 'initialize': result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'api_schema_only', 'version': '1'}}
                elif method == 'tools/list': result = {'tools': [{'name': t['function']['name'], 'description': t['function']['description'], 'inputSchema': t['function']['parameters']} for t in registry.tools]}
                elif method != 'server/discover':
                    self.reply(200, {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'Metadata operation not supported'}}); return
                self.reply(200, {'jsonrpc': '2.0', 'id': rid, 'result': result})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True); self.thread.start()
    def mixin_fields(self):
        provider = field(1, 2, 'api_client') + field(2, 2, f'http://127.0.0.1:{self.server.server_port}{self.path}') + field(3, 0, 1)
        items = b''
        for tool in self.tools:
            fn = tool['function']; definition = field(1, 2, fn['name']) + field(2, 2, field(1, 2, fn['description']) + field(2, 2, json.dumps(fn['parameters'], ensure_ascii=False)))
            provider += field(7, 2, definition)
            items += field(3, 2, field(1, 2, fn['name']) + field(3, 2, 'api_client'))
        return field(1, 2, provider) + items
    def close(self):
        self.cancelled.set(); self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
