"""Private HTTP listeners and thin standalone native API adapter."""
import argparse
import errno
import fcntl
import json
import os
import signal
import socket
import stat
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer
from . import account_store as accounts
from .application import Application, MODELS
from .core import BridgeError, validate
from .native import NativeBackend
from .http_transport import TransportHandler
from .execution import serve_native

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, app, token):
        if address[0] != '127.0.0.1': raise ValueError('Only literal IPv4 loopback binding is allowed')
        self.app, self.token = app, token
        if len(token) < 24: raise ValueError('API token must have at least 24 characters')
        self.connection_slots = threading.BoundedSemaphore(48)
        super().__init__(address, Handler)
    def process_request(self, request, address):
        if not self.connection_slots.acquire(blocking=False):
            try: request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally: self.shutdown_request(request)
            return
        try: super().process_request(request, address)
        except BaseException: self.connection_slots.release(); raise
    def process_request_thread(self, request, address):
        try: super().process_request_thread(request, address)
        finally: self.connection_slots.release()

class UnixServer(Server):
    """Private AF_UNIX listener; same authentication, connection cap and handlers."""
    address_family = socket.AF_UNIX

    def __init__(self, path, app, token):
        self.path = Path(os.path.abspath(path))
        self.socket_identity = None
        self.socket_lock = None
        self.app, self.token = app, token
        if len(token) < 24:
            raise ValueError('API token must have at least 24 characters')
        if len(os.fsencode(self.path)) > 103:
            raise ValueError('Unix socket path is too long')
        parent = self.path.parent
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = parent.stat()
        if parent.resolve() != parent or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Unix socket directory must be owned, private (0700), and without symlink components')
        fd = os.open(parent/('.' + self.path.name + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.socket_lock = fd
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('Unsafe Unix socket lock')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Unix socket already has a server') from None
            if self.path.exists() or self.path.is_symlink():
                old = self.path.lstat()
                if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.getuid():
                    raise ValueError('Refusing to remove unsafe Unix socket path')
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(.2)
                    try:
                        probe.connect(str(self.path))
                    except OSError as exc:
                        if exc.errno != errno.ECONNREFUSED:
                            raise ValueError('Unix socket is not proven stale') from None
                    else:
                        raise ValueError('Unix socket is active')
                current = self.path.lstat()
                if (current.st_dev, current.st_ino) != (old.st_dev, old.st_ino):
                    raise ValueError('Unix socket changed while checking')
                self.path.unlink()
            self.connection_slots = threading.BoundedSemaphore(48)
            ThreadingHTTPServer.__init__(self, str(self.path), Handler)
        except BaseException:
            self.server_close()
            raise

    def server_bind(self):
        # HTTPServer.server_bind assumes an INET tuple; AF_UNIX has a path.
        self.socket.bind(self.server_address)
        info = self.path.lstat()
        self.socket_identity = (info.st_dev, info.st_ino)
        os.chmod(self.path, 0o600)
        self.server_name, self.server_port = 'localhost', 0

    def server_close(self):
        if hasattr(self, 'socket'):
            super().server_close()
        if self.socket_identity is not None:
            try:
                info = self.path.lstat()
                if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self.socket_identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self.socket_identity = None
        if self.socket_lock is not None:
            os.close(self.socket_lock)
            self.socket_lock = None

class Handler(TransportHandler):
    def do_GET(self):
        if not self.authorized(): return self.reply(401, BridgeError(401, 'unauthorized', 'Unauthorized').public())
        if self.path == '/v1/models': return self.reply(200, {'object': 'list', 'data': [{'id': m, 'object': 'model', 'created': 0, 'owned_by': 'antigravity-cli'} for m in self.server.app.models]})
        if self.path == '/healthz': return self.reply(200, {'ok': not self.server.app.scheduler.draining, **self.server.app.status()})
        return self.reply(404, BridgeError(404, 'not_found', 'Not found').public())
    def do_POST(self):
        if not self.authorized(): return self.reply(401, BridgeError(401, 'unauthorized', 'Unauthorized').public())
        if self.path != '/v1/chat/completions': return self.reply(404, BridgeError(404, 'not_found', 'Not found').public())
        try:
            app = self.application()
            req = validate(self.read_json(), app.models)
            snap = app.resolve(self.headers.get('X-AGY-Account'))
        except BridgeError as exc:
            return self.reply(exc.status, exc.public(), {'X-AGY-Dispatch': 'not-submitted'})
        return serve_native(self, app, req, snapshot=snap)


def main():
    p = argparse.ArgumentParser(description='Local-only Antigravity text/function API bridge')
    p.add_argument('--port', type=int, default=17861); p.add_argument('--unix-socket', type=Path); p.add_argument('--account', help='Bind every request to this account alias'); p.add_argument('--root', type=Path, default=Path.home()/'.local/share/agy-api-bridge')
    p.add_argument('--accounts-root', type=Path, default=accounts.DEFAULT_ROOT); p.add_argument('--enable-native', action='store_true')
    p.add_argument('--account-concurrency', type=int, choices=(1, 2), default=2); p.add_argument('--concurrency', type=int, default=2); p.add_argument('--queue-size', type=int, default=24); p.add_argument('--timeout', type=float, default=0); p.add_argument('--queue-timeout', type=float, default=0); p.add_argument('--drain-timeout', type=float, default=0)
    args = p.parse_args()
    if not 1 <= args.concurrency <= 8 or not 1 <= args.queue_size <= 128 or min(args.timeout, args.queue_timeout, args.drain_timeout) < 0: p.error('Invalid limits')
    accounts.private_dir(args.root)
    server_lock = os.open(args.root/'server.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try: fcntl.flock(server_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: os.close(server_lock); p.error('This bridge runtime already has a server')
    token_path = args.root/'api-key'
    if token_path.is_symlink(): p.error('Unsafe key file')
    if not token_path.exists(): accounts.atomic_bytes(token_path, (uuid.uuid4().hex + uuid.uuid4().hex).encode())
    if token_path.stat().st_mode & 0o077: p.error('Key file must be private')
    store = accounts.Accounts(args.accounts_root)
    backend = NativeBackend(args.root/'runtime', store) if args.enable_native else None
    app = Application(args.root, store, backend, args.enable_native, concurrency=args.concurrency, max_queue=args.queue_size, timeout=args.timeout, queue_timeout=args.queue_timeout, per_account=args.account_concurrency, fixed_account=args.account)
    token = token_path.read_text().strip()
    server = UnixServer(args.unix_socket, app, token) if args.unix_socket else Server(('127.0.0.1', args.port), app, token)
    stopping = threading.Event()
    def shutdown(signum, frame):
        if stopping.is_set(): return
        stopping.set(); app.scheduler.drain()
        def drain():
            deadline = time.monotonic() + args.drain_timeout
            while app.scheduler.status()['active'] and (args.drain_timeout == 0 or time.monotonic() < deadline): time.sleep(.1)
            if app.scheduler.status()['active']: app.force_cancel.set()
            cleanup_deadline = time.monotonic() + 15
            while app.scheduler.status()['active'] and time.monotonic() < cleanup_deadline: time.sleep(.1)
            server.shutdown()
        threading.Thread(target=drain, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown); signal.signal(signal.SIGINT, shutdown)
    print(json.dumps({'listen': f'unix:{server.path}' if args.unix_socket else f'http://127.0.0.1:{server.server_port}/v1', 'key_file': str(token_path), **app.status()}, ensure_ascii=False), flush=True)
    try: server.serve_forever(poll_interval=.1)
    finally: server.server_close(); os.close(server_lock)

if __name__ == '__main__': main()
