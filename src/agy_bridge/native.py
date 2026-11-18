"""Pinned CLI transport. Native subscription success remains unqualified.
Only the loopback Gemini mock route is tested end-to-end without real inference.
"""
import hashlib
import http.client
import json
import os
import queue
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from . import account_store as accounts
from .core import BridgeError, upstream_error, exposed_tools, allowed_tool_names, forced_tool_name, native_tool_choice_supported
from .protocol import seed, field, fields, account_session_ids
from .system_timeline import split_system_messages
from .tool_schema import SchemaRegistry
from . import credentials
from .error_detail import redact
from .event_queue import EventQueue

PIN = accounts.BINARY_SHA
_BINARY_HASH_CACHE = {}
_BINARY_HASH_LOCK = threading.Lock()

def verify_binary(binary):
    # Stream the 269 MiB binary rather than allocating it as one bytes object.
    # Cache only root-owned non-writable files on a read-only filesystem.
    # Mutable temp/host files can change twice within one filesystem timestamp tick.
    binary = Path(binary)
    stat = binary.stat()
    signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    key = str(binary.absolute())
    immutable = stat.st_uid == 0 and not stat.st_mode & 0o222 and bool(os.statvfs(binary).f_flag & os.ST_RDONLY)
    with _BINARY_HASH_LOCK:
        if immutable and _BINARY_HASH_CACHE.get(key) == signature:
            return
        digest = hashlib.sha256()
        with binary.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        after = binary.stat()
        if signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('CLI binary changed during verification')
        if digest.hexdigest() != PIN:
            raise ValueError('Pinned CLI checksum mismatch')
        if immutable: _BINARY_HASH_CACHE[key] = signature
        else: _BINARY_HASH_CACHE.pop(key, None)

def worker_namespace_args():
    mode = os.environ.get('AGY_CLI_PROC_MOUNT', '1')
    if mode not in ('0', '1'):
        raise ValueError('AGY_CLI_PROC_MOUNT must be 0 or 1')
    args = ['unshare', '-Urpf', '--kill-child=TERM']
    if mode == '1': args.append('--mount-proc')
    return args

_enum_file = Path(os.environ.get('AGY_MODEL_ENUMS', Path(__file__).parent / 'fixtures/model-enums.local.json'))
MODEL_ENUMS = json.loads(_enum_file.read_text()) if _enum_file.is_file() else {}

def get(b, key): return next(v for n, w, v in fields(b) if n == key)
def change(b, key, wire, value): return b''.join(field(n, w, v) for n, w, v in fields(b) if n != key) + field(key, wire, value)

class NativeBackend:
    def __init__(self, runtime, store, binary=accounts.BINARY, mock_upstream=None):
        self.runtime, self.store, self.binary, self.mock_upstream = Path(runtime), store, Path(binary), mock_upstream
        accounts.private_dir(self.runtime)
        verify_binary(self.binary)
        worker_namespace_args()  # Validate operator configuration before accepting work.
        if mock_upstream:
            from urllib.parse import urlsplit
            u = urlsplit(mock_upstream)
            if u.scheme != 'http' or u.hostname != '127.0.0.1' or [n for _, n in socket.if_nameindex()] != ['lo']:
                raise ValueError('Mock mode requires a loopback-only network namespace')
    def session(self, body, snapshot, check):
        if not native_tool_choice_supported(body):
            raise BridgeError(501, 'unsupported_tool_option', 'Multi-function required mode needs the compatibility backend; no CLI request was submitted')
        return Session(self, body, snapshot, check)

class Session:
    def __init__(self, backend, body, snapshot, check):
        self.backend, self.body, self.snapshot, self.check = backend, body, snapshot, check
        self.temp = tempfile.TemporaryDirectory(prefix='request-', dir=backend.runtime)
        self.base = Path(self.temp.name); self.home = self.base/'home'; self.work = self.base/'work'
        self.q = EventQueue(); self.ready = threading.Event(); self.proc = self.conn = self.reader = self.port = self.stream_socket = None
        self.reader_failure = None
        self.closed = self.cancelled = self.started = self.done = self.response_done = self.finish_seen = False
        self.tool_handoff = False; self.registry = None; self.tool_calls = []; self.tool_deltas = []; self.tool_steps_done = set()
        self.text = ''; self.response_steps = {}; self.error = None; self.usage = None; self.finish = 'stop'; self.original_auth = {}
    def post(self, name, body, proto=False, *, cleanup=False):
        # Request RPCs have no time deadline. Cancellation interrupts their socket.
        # A short deadline is used ONLY after completion/cancellation, for teardown.
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3 if cleanup else None)
        stop = threading.Event(); failed = []; transport = None; response = None
        def watch():
            while not stop.wait(.05):
                try: self.check()
                except BridgeError as exc:
                    failed.append(exc)
                    try:
                        if transport: transport.shutdown(socket.SHUT_RDWR)
                        elif c.sock: c.sock.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    c.close(); return
        thread = None if cleanup else threading.Thread(target=watch, daemon=True)
        if thread: thread.start()
        try:
            if not cleanup: self.check()
            c.connect(); transport = socket.socket(fileno=os.dup(c.sock.fileno()))
            if not cleanup: self.check()
            c.request('POST', '/exa.language_server_pb.LanguageServerService/' + name, body if proto else json.dumps(body).encode(), {'Content-Type': 'application/proto' if proto else 'application/json', 'Connect-Protocol-Version': '1', 'x-codeium-csrf-token': self.csrf})
            response = c.getresponse(); data = response.read(20000000)
            if not cleanup: self.check()
            if response.status != 200:
                (self.base/'control-error.log').write_text(str(response.status)+' '+data.decode(errors='replace'))
                error = upstream_error(data.decode(errors='replace'), self.error_secrets())
                details = dict(error.details, source='cli_control', upstream_status=response.status, status_source='cli_control_http')
                raise BridgeError(502, 'cli_control_error', error.message if details.get('original_message_present') else 'Local CLI control request failed', **details)
            return data
        except (OSError, http.client.HTTPException):
            if failed: raise failed[0]
            raise
        finally:
            stop.set(); c.close()
            if response: response.close()
            if transport: transport.close()
            if thread: thread.join(timeout=2)
    def start(self):
        self.check()
        system, h = split_system_messages(self.body['messages'])
        self.last = h[-1]['content'] if h[-1]['role'] == 'user' else None
        self.history = h[:-1] if self.last is not None else h
        self.home.mkdir(mode=0o700); agents = self.work/'.agents/agents'; agents.mkdir(parents=True, mode=0o700)
        (agents/'plain-chat.md').write_text('---\nname: plain-chat\ndescription: Isolated text API.\nmainAgent: true\nsubagent: false\nexcludeDefaultComponents: true\ntools: []\n---\n' + system + '\n')
        self.cid, self.tid = account_session_ids(self.snapshot.alias, self.snapshot.expected_email)
        seed(self.home, self.work, system, self.history, session_ids=(self.cid, self.tid))
        self.csrf = uuid.uuid4().hex
        staged = accounts.Snapshot(self.snapshot.alias, self.home, self.work, self.snapshot.generation, self.snapshot.expected_email)
        if self.backend.mock_upstream:
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.home), 'GEMINI_API_KEY': 'offline-not-a-key', 'GOOGLE_GEMINI_BASE_URL': self.backend.mock_upstream, 'NO_PROXY': '127.0.0.1,localhost', 'LANG': 'C.UTF-8', 'AGY_CLI_DISABLE_AUTO_UPDATE': 'true'}
            settings = {'modelProvider': 'gemini', 'useG1Credits': False}
        else:
            env = accounts.environment(staged)
            try: self.original_auth = credentials.stage(self.snapshot, self.home, self.backend.store)
            except accounts.AccountError as e: raise BridgeError(503, 'unsafe_credentials', str(e)) from None
            settings = {'useG1Credits': False}
        exposed_names = [t['function']['name'] for t in exposed_tools(self.body)]
        # Permission is limited to the inert local handoff endpoint, never to a
        # shell/file/network tool. This avoids CLI auto-denial truncating the model
        # stream before all parallel calls have arrived.
        if exposed_names: settings['permissions'] = {'allow': ['mcp(api_client/' + n + ')' for n in exposed_names]}
        self.upstream_transport_mode='native-default'
        if not self.backend.mock_upstream and self.body['model'] in getattr(self.backend,'nonstream_models',()):
            ca=self.backend.nonstream_state/'trust.pem'
            try:
                if not ca.is_file():raise OSError('Missing adapter trust bundle')
                with socket.create_connection(('127.0.0.1',17863),timeout=2):pass
            except OSError:
                raise BridgeError(502,'upstream_transport_unavailable','The configured local nonstream transport is unavailable; no model request was submitted') from None
            self.nonstream_request_id=uuid.uuid4().hex
            self.nonstream_registration=self.backend.nonstream_state/'registrations'/self.nonstream_request_id
            self.nonstream_error_file=self.backend.nonstream_state/'errors'/(self.nonstream_request_id+'.json')
            self.nonstream_registration.touch(mode=0o600)
            env['HTTPS_PROXY']=env['https_proxy']='http://'+self.nonstream_request_id+':local@127.0.0.1:17863'
            env['NO_PROXY']=env['no_proxy']='127.0.0.1,localhost,::1'
            env['SSL_CERT_FILE']=str(ca)
            self.upstream_transport_mode='google-nonstream-json'
        env['ANTIGRAVITY_SERVER_CSRF_TOKEN'] = self.csrf
        env['AGY_BRIDGE_REQUEST_DIR'] = str(self.base)
        args = [str(self.backend.binary), '--settings', json.dumps(settings), '--conversation', self.cid, '--agent', 'plain-chat', '--model', self.body['model'], '--input-format', 'stream-json', '--output-format', 'stream-json', '--log-file', str(self.base/'cli.log')]
        self.stdout = (self.base/'stdout.log').open('w'); self.stderr = (self.base/'stderr.log').open('w')
        self.check()
        self.proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name('guard.py')), str(os.getpid()), *worker_namespace_args(), *args], cwd=self.work, env=env, stdin=subprocess.PIPE, stdout=self.stdout, stderr=self.stderr, start_new_session=True, umask=0o077, pass_fds=getattr(getattr(self, 'lease', None), 'fds', ()))
        while True:
            self.check()
            failure = self.startup_error()
            if failure: raise failure
            p = self.base/'cli.log'; text = p.read_text(errors='replace') if p.exists() else ''
            match = re.search(r'listening on random port at (\d+) for HTTP\n', text)
            if match: self.port = int(match[1]); break
            if self.proc.poll() is not None: raise self.startup_error() or BridgeError(502, 'cli_start_failed', 'CLI exited before local control was ready')
            time.sleep(.05)
        self.wait_initialization()
        self.post('GetCascadeTrajectory', {'cascadeId': self.cid})
        self.reader = threading.Thread(target=self.listen, daemon=True); self.reader.start()
        while not self.ready.wait(.05):
            self.check()
            if self.proc.poll() is not None: raise self.startup_error() or BridgeError(502, 'cli_exited', 'CLI exited before its event stream was ready')
        if self.reader_failure: raise self.reader_failure
        db = sqlite3.connect(self.home/'.gemini/antigravity-cli/conversations'/f'{self.cid}.db')
        try: meta = db.execute('select data from trajectory_metadata_blob where id="main"').fetchone()[0]
        finally: db.close()
        mixin = change(get(get(get(get(meta, 4), 7), 18), 1), 10, 0, 1)
        # Retain only caller prompt plus the pinned internal stop-control hooks.
        # These hooks are not model-visible tools. Removing them disables the
        # max-invocation and terminal-step gates (confirmed by offline traces).
        stop_hooks = {b'message_continue_check', b'terminal_step_check', b'max_generator_invocations_check', b'empty_output_continuation_check', b'force_invocation', b'no_tool_call_check'}
        mixin = b''.join(field(n, w, v) for n, w, v in fields(mixin) if n in (2, 9, 10) or (n == 5 and w == 2 and get(v, 1) in stop_hooks and get(v, 2) == 1))
        exposed = exposed_tools(self.body)
        if exposed:
            self.registry = SchemaRegistry(exposed); mixin += self.registry.mixin_fields()
        # Resolve exact CLI model ID from its current local catalog. Never guess an enum.
        model = None
        while model is None:
            self.check()
            catalog = json.loads(self.post('GetCascadeModelConfigData', {}))
            quota_store = getattr(self.backend, 'quota_store', None)
            if quota_store:
                try: quota_store.observe_catalog(self.snapshot.alias, catalog)
                except (OSError, ValueError, TypeError): pass  # Metadata I/O is not a generation failure.
            for item in catalog.get('clientModelConfigs', []):
                if item.get('modelId') == self.body['model']:
                    if item.get('disabled') is True:
                        raise BridgeError(503, 'model_unavailable', 'Selected model is disabled in the official CLI catalog')
                    model = MODEL_ENUMS.get(item.get('modelOrAlias', {}).get('model'))
            if model is None:
                if catalog.get('clientModelConfigs'): raise BridgeError(503, 'model_unavailable', 'Requested model is absent from the selected account CLI catalog')
                if self.proc.poll() is not None: raise self.startup_error() or BridgeError(502, 'cli_exited', 'CLI exited before model catalog was ready')
                time.sleep(.2)
        if getattr(self.backend, 'quota_store', None):
            try: standing = self.backend.quota_store.standing(self.snapshot.alias, self.body['model'])
            except (OSError, ValueError, TypeError): standing = {'blocked_until': None}
            if standing['blocked_until'] is not None:
                raise BridgeError(429, 'quota_exhausted', 'Official CLI metadata reports exhausted quota; no model request was submitted',
                                  retry_after=max(1, int(standing['blocked_until']-time.time())+1), dispatch_blocked=True)
        # With zero retries the initial attempt is also the last attempt. The
        # CLI default last-retry option forbids tools; override it explicitly.
        # Named forcing is verified on the FIRST and ONLY outgoing request.
        forced = forced_tool_name(self.body)
        output_retry = field(1, 0, 0) + (field(4, 2, forced) if forced else field(3, 0, 0 if exposed else 1))
        retry = field(1, 2, output_retry) + field(2, 2, field(1, 0, 0))
        planner = field(1, 0, model) + field(51, 0, 0) + field(14, 0, 900000) + field(46, 2, mixin) + field(13, 2, field(49, 0, 0 if exposed else 1)) + field(33, 0, 1) + field(7, 0, 1) + field(6, 0, self.body['max_tokens']) + field(56, 2, field(2, 0, self.body['max_tokens'])) + field(30, 2, retry)
        conversion = field(17, 0, 400000) + field(13, 0, 400000) + field(9, 0, 1) + field(20, 0, 1) + field(21, 0, 0) + field(8, 0, 1) + field(10, 0, 0) + field(5, 0, 0)
        cfg = field(1, 2, planner) + field(2, 2, field(5, 0, 900000) + field(1, 0, 800000) + field(36, 2, '900K')) + field(3, 2, field(1, 0, 0) + field(2, 0, 1) + field(14, 0, 1) + field(23, 0, 0) + field(3, 0, 15) + field(24, 0, 1) + field(16, 0, 0) + field(17, 0, 0)) + field(4, 2, conversion)
        self.check(); self.started = True
        self.post('SendUserCascadeMessage', field(1, 2, self.cid) + (field(2, 2, field(1, 2, self.last)) if self.last is not None else b'') + field(5, 2, cfg) + field(8, 0, 0) + field(16, 0, 1), True)
    def error_secrets(self):
        values = [getattr(self, 'csrf', '')]
        for raw in getattr(self, 'original_auth', {}).values():
            try: data = json.loads(raw)
            except (ValueError, UnicodeError): continue
            pending = [data]
            while pending:
                item = pending.pop()
                if isinstance(item, dict):
                    for key, value in item.items():
                        if key.lower() in ('access_token', 'refresh_token', 'id_token', 'api_key', 'client_secret') and isinstance(value, str): values.append(value)
                        elif isinstance(value, (dict, list)): pending.append(value)
                elif isinstance(item, list): pending.extend(item)
        return values

    def stdout_events(self):
        # Startup JSON is bounded and local. Never expose arbitrary stdout/raw
        # traces through the API; only the allowlisted error normalizer may run.
        try:
            with (self.base/'stdout.log').open('rb') as f:
                f.seek(0, 2); end = f.tell(); f.seek(max(0, end - 262144))
                raw = f.read(262144)
        except FileNotFoundError:
            return []
        events = []
        for line in raw.splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict): events.append(event)
            except (ValueError, UnicodeError): pass
        return events

    def startup_error(self):
        for event in reversed(self.stdout_events()):
            result = event.get('result')
            if event.get('event') == 'result' and isinstance(result, dict) and result.get('status') == 'ERROR':
                return upstream_error(result.get('error', ''), self.error_secrets())
        return None

    def wait_initialization(self):
        # The control socket/catalog can become available BEFORE the official
        # CLI eligibility check finishes. Wait for its own init event before
        # submitting any conversation RPC; never race or bypass that gate.
        while True:
            self.check()
            error = self.startup_error()
            if error: raise error
            for event in self.stdout_events():
                if event.get('event') == 'init' and event.get('conversation_id') == self.cid:
                    if (event.get('init') or {}).get('model') != self.body['model']:
                        raise BridgeError(503, 'model_unavailable', 'Official CLI initialized a different model; no fallback is permitted')
                    return
            if self.proc.poll() is not None:
                raise self.startup_error() or BridgeError(502, 'cli_start_failed', 'CLI exited before official initialization completed')
            time.sleep(.05)

    def listen(self):
        def put(e, size=None):
            while not self.cancelled:
                try: self.q.put(e, timeout=.2, size=size); return
                except queue.Full: pass
        try:
            c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=None); self.conn = c
            c.connect(); self.stream_socket = socket.socket(fileno=os.dup(c.sock.fileno()))
            obj = json.dumps({'conversationId': self.cid, 'subscriberId': uuid.uuid4().hex}).encode()
            c.request('POST', '/exa.language_server_pb.LanguageServerService/StreamAgentStateUpdates', b'\0' + len(obj).to_bytes(4, 'big') + obj, {'Content-Type': 'application/connect+json', 'Connect-Protocol-Version': '1', 'x-codeium-csrf-token': self.csrf})
            r = c.getresponse()
            if r.status != 200: raise ValueError('stream HTTP error')
            while not self.cancelled:
                hdr = r.read(5)
                if len(hdr) != 5: raise ValueError('truncated stream header')
                size = int.from_bytes(hdr[1:], 'big')
                if size > 16000000: raise ValueError('oversize stream frame')
                data = r.read(size)
                if len(data) != size: raise ValueError('truncated stream frame')
                e = json.loads(data)
                if hdr[0] & 2:
                    if e.get('error'): put({'reader_error': True, 'upstream_error': e['error']})
                    break
                put(e, size); self.ready.set()
        except Exception as exc:
            if not self.cancelled:
                self.reader_failure = BridgeError(502, 'incomplete_stream', 'CLI event stream failed', source='bridge_transport', reader_detail=redact(str(exc), self.error_secrets())[0], exception_type=type(exc).__name__)
                put({'reader_error': True, 'reader_detail': redact(str(exc), self.error_secrets())[0], 'exception_type': type(exc).__name__})
        finally:
            if not self.cancelled: put({'reader_end': True})
            self.ready.set()
    def check_nonstream_error(self):
        path=getattr(self,'nonstream_error_file',None)
        if path and path.is_file():
            e=json.loads(path.read_text())
            raise BridgeError(e['status'],e['code'],e['message'],**e.get('details',{}))
    def next(self, timeout=.1):
        self.check()
        self.check_nonstream_error()
        try: e = self.q.get(timeout=timeout)
        except queue.Empty:
            if self.proc and self.proc.poll() is not None: raise self.startup_error() or BridgeError(502, 'cli_exited', 'CLI exited before a completed response')
            return ''
        self.check_nonstream_error()
        if ('reader_error' in e or 'reader_end' in e) and not self.done:
            if e.get('upstream_error'): raise upstream_error(e['upstream_error'], self.error_secrets())
            if self.error: raise upstream_error(self.error, self.error_secrets())
            raise self.startup_error() or BridgeError(502, 'incomplete_stream', 'CLI event stream ended before a completed response', source='bridge_transport', **{k:e[k] for k in ('reader_detail','exception_type') if e.get(k)})
        u = e.get('update', {}); t = u.get('mainTrajectoryUpdate', {}); su = t.get('stepsUpdate', {}); delta = ''
        for idx, s in zip(su.get('indices', []), su.get('steps', [])):
            if idx < len(self.history): continue
            if s.get('type') == 'CORTEX_STEP_TYPE_PLANNER_RESPONSE':
                p = s.get('plannerResponse', {}); value = p.get('response', '')
                if p.get('toolCalls') and s.get('status') == 'CORTEX_STEP_STATUS_DONE' and idx not in self.tool_steps_done:
                    allowed = allowed_tool_names(self.body)
                    if len(p['toolCalls']) > 128: raise BridgeError(502, 'too_many_tool_calls', 'CLI returned more than 128 tool calls')
                    for call in p['toolCalls']:
                        if call.get('name') not in allowed: raise BridgeError(502, 'unexpected_tool', 'Model returned a tool not exposed by this request')
                        cid = call.get('id')
                        if not cid or any(c['id'] == cid for c in self.tool_calls): raise BridgeError(502, 'invalid_tool_call', 'CLI returned a missing or duplicate tool call ID')
                        value_call = {'id': cid, 'type': 'function', 'function': {'name': call['name'], 'arguments': call.get('argumentsJson') or call.get('invalidJsonStr') or '{}'}}
                        self.tool_deltas.append({'index': len(self.tool_calls), **value_call}); self.tool_calls.append(value_call)
                    self.tool_steps_done.add(idx); self.finish = 'tool_calls'
                prev = self.response_steps.get(idx, '')
                if not value.startswith(prev): raise BridgeError(502, 'nonprefix_output', 'CLI changed already streamed text')
                delta += value[len(prev):]; self.response_steps[idx] = value
                mu = s.get('metadata', {}).get('modelUsage', {})
                if mu:
                    self.usage = {'prompt_tokens': int(mu.get('inputTokens', 0)), 'completion_tokens': int(mu.get('outputTokens', 0))}
                    self.usage['total_tokens'] = sum(self.usage.values())
                if s.get('status') == 'CORTEX_STEP_STATUS_DONE': self.response_done = True; self.finish_seen = bool(p.get('stopReason')) and not any(x in p['stopReason'] for x in ('ERROR', 'CANCEL', 'UNSPECIFIED'))
                if 'MAX' in p.get('stopReason', '') or 'LENGTH' in p.get('stopReason', ''): self.finish = 'length'
            if 'ERROR' in s.get('type', '') or s.get('status') == 'CORTEX_STEP_STATUS_ERROR': self.error = s
        # A completed planner response is the API terminal boundary. Later agent
        # dispatch/validation steps are not part of this API request. No tool result
        # can be supplied by our schema-only registry; close() cancels the process.
        if self.tool_calls and self.response_done and not self.finish_seen:
            raise BridgeError(502, 'missing_finish', 'Tool call stream ended without a successful terminal finish reason')
        if self.tool_calls and self.response_done and self.finish_seen:
            if len(self.text) + len(delta) + sum(len(c['function']['arguments']) for c in self.tool_calls) > 8000000: raise BridgeError(502, 'output_too_large', 'CLI output exceeded the bridge safety bound')
            self.done = self.tool_handoff = True
            self.text += delta
            return delta
        if t.get('lastStepError'): self.error = {'step': self.error, 'lastStepError': t['lastStepError']}
        if self.started and u.get('fullyIdle'):
            if self.error and self.finish != 'length': raise upstream_error(self.error, self.error_secrets())
            if self.response_done:
                if not self.finish_seen: raise BridgeError(502, 'missing_finish', 'Upstream ended without a terminal finish reason')
                # Preserve a completed model response even when it did not follow
                # the caller's tool choice. Do not manufacture a bridge error.
                self.done = True
        self.text += delta
        if len(self.text) > 8000000: raise BridgeError(502, 'output_too_large', 'CLI output exceeded the bridge safety bound')
        return delta
    def close(self, cancel=False):
        if self.closed: return
        self.closed = self.cancelled = True
        try:
            if (cancel or self.tool_handoff) and self.port:
                try: self.post('CancelCascadeInvocation', {'cascadeId': self.cid, 'killBackgroundTasks': True}, cleanup=True)
                except Exception: pass
            if self.proc:
                try: os.killpg(self.proc.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try: os.killpg(self.proc.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    self.proc.wait(timeout=3)
                if self.proc.stdin: self.proc.stdin.close()
            for name in ('stdout', 'stderr'):
                if hasattr(self, name): getattr(self, name).close()
            if self.stream_socket:
                try: self.stream_socket.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                self.stream_socket.close()
            if self.conn: self.conn.close()
            if self.reader: self.reader.join(timeout=2)
            # Shared request leases are separate from the short CAS publication lock.
            if not self.backend.mock_upstream and self.original_auth:
                self.credential_writeback = credentials.publish(self.snapshot, self.home, self.original_auth, self.backend.store)
        finally:
            if self.registry: self.registry.close()
            for name in ('nonstream_registration','nonstream_error_file'):
                path=getattr(self,name,None)
                if path:path.unlink(missing_ok=True)
            self.temp.cleanup()
