"""One native execution lifecycle, independent of route selection and parsing."""
import json
import socket
import time
import uuid
from .core import BridgeError, StopFilter
from .error_detail import redact


def serve_native(transport, app, req, *, snapshot=None, external_check=None, audit_extra=None):
    # The transport contract is deliberately small: gone/reply/send_header and
    # the output stream. No HTTP handler inheritance or recursive do_POST.
    self = transport
    rid = 'chatcmpl-' + uuid.uuid4().hex; start = time.monotonic(); created = int(time.time())
    headers = False; session = None; lease = None; acquired = False; success = False; local_stop = False; snap = snapshot; code = 'unknown'; text = ''
    def check():
        if external_check: external_check()
        if self.gone(): raise BridgeError(499, 'client_disconnected', 'Client disconnected')
        if app.force_cancel.is_set(): raise BridgeError(503, 'shutdown_timeout', 'Server drain deadline reached')
        if app.timeout > 0 and time.monotonic() - start > app.timeout: raise BridgeError(504, 'timeout', 'Request deadline exceeded')
        if app.queue_timeout > 0 and not acquired and time.monotonic() - start > app.queue_timeout: raise BridgeError(504, 'queue_timeout', 'Queue wait deadline exceeded')
    def send(obj):
        self.wfile.write(('data: ' + (obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)) + '\n\n').encode()); self.wfile.flush()
    def chunk(delta, finish=None): return {'id': rid, 'object': 'chat.completion.chunk', 'created': created, 'model': req.body['model'], 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
    try:
        if snap is None: snap = app.resolve()
        if not app.enabled: raise BridgeError(503, 'inference_disabled', 'Native inference is disabled; no CLI request was started')
        lease = app.scheduler.acquire(rid, snap.alias, check, app.leases); acquired = True
        app.check_account(snap.alias, req.body['model']); check()
        session = app.backend.session(req.body, snap, check); session.lease = lease; session.start(); check()
        stream = req.body['stream']; filt = StopFilter(req.stops)
        extra = {'X-Request-ID': rid, 'X-AGY-Account': snap.alias, 'X-AGY-Ignored-Parameters': ','.join(req.ignored)}
        if stream:
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.send_header('Cache-Control', 'no-cache'); self.send_header('Connection', 'close'); self.send_header('X-Accel-Buffering', 'no')
            for k, v in extra.items(): self.send_header(k, v)
            self.end_headers(); headers = True; send(chunk({'role': 'assistant'}))
        heartbeat = time.monotonic()
        while not session.done:
            check(); delta = filt.feed(session.next()); text += delta
            if delta and stream: send(chunk({'content': delta}))
            if stream and not filt.hit and getattr(session, 'tool_deltas', None):
                send(chunk({'tool_calls': session.tool_deltas})); session.tool_deltas = []
            if filt.hit: local_stop = True; break
            if stream and time.monotonic() - heartbeat > 10:
                self.wfile.write(b': keep-alive\n\n'); self.wfile.flush(); heartbeat = time.monotonic()
        delta = filt.feed('', final=True); text += delta
        if delta and stream: send(chunk({'content': delta}))
        finish = 'stop' if local_stop else session.finish
        if stream:
            send(chunk({}, finish))
            if req.include_usage: send({'id': rid, 'object': 'chat.completion.chunk', 'created': created, 'model': req.body['model'], 'choices': [], 'usage': session.usage})
            send('[DONE]')
        else:
            self.reply(200, {'id': rid, 'object': 'chat.completion', 'created': created, 'model': req.body['model'], 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text if local_stop or text or not getattr(session, 'tool_calls', []) else None, **({'tool_calls': session.tool_calls} if not local_stop and getattr(session, 'tool_calls', []) else {})}, 'finish_reason': finish}], 'usage': session.usage}, extra)
        success = True; code = 'ok'
    except Exception as e:
        if not isinstance(e, BridgeError):
            if isinstance(e, (BrokenPipeError, ConnectionResetError)):
                if self.gone(): e = BridgeError(499, 'client_disconnected', 'Client disconnected')
                else: e = BridgeError(502, 'transport_error', 'Bridge transport failed: '+redact(str(e), [self.server.token])[0], source='bridge_transport', exception_type=type(e).__name__)
            elif req is None and isinstance(e, (ValueError, UnicodeError)): e = BridgeError(400, 'invalid_request', 'Invalid JSON or request encoding')
            elif isinstance(e, (TimeoutError, socket.timeout)): e = BridgeError(504, 'timeout', 'Request timed out')
            else:
                known = [self.server.token] + (session.error_secrets() if session and hasattr(session, 'error_secrets') else [])
                detail, redacted, truncated = redact(str(e), known)
                e = BridgeError(500, 'internal_error', 'Bridge internal error: '+(detail or type(e).__name__), exception_type=type(e).__name__, message_redacted=redacted, message_truncated=truncated)
        code = e.code
        e.details['request_id'] = rid
        e.details['model_request_submitted'] = bool(session and getattr(session, 'started', True))
        if snap: app.outcome(snap.alias, e, req.body['model'])
        try:
            if headers: send(e.public()); send('[DONE]')
            elif e.status != 499:
                extra = {'X-Request-ID': rid, 'X-AGY-Dispatch': 'submitted-or-uncertain' if session and getattr(session, 'started', True) else 'not-submitted'}
                if 'retry_after' in e.details: extra['Retry-After'] = e.details['retry_after']
                self.reply(e.status, e.public(), extra)
        except OSError: pass
    finally:
        cleanup = 'ok'
        try:
            if session: session.close(cancel=not success or local_stop)
        except Exception: cleanup = 'failed'
        finally:
            if lease is not None: lease.close()
        self.completion_result = code
        try:
            app.audit({'id': rid, 'account': snap.alias if snap else None, 'selection_generation': snap.generation if snap else None, 'model': req.body['model'] if req else None, 'stream': req.body['stream'] if req else None, 'ignored_parameters': list(req.ignored) if req else [], 'seconds': round(time.monotonic() - start, 3), 'output_chars': len(text), 'tool_call_count': len(getattr(session, 'tool_calls', [])) if session else 0, 'tool_handoff': bool(getattr(session, 'tool_handoff', False)), 'session_identity_mode': 'account-v1' if session else None, 'conversation_id': getattr(session, 'cid', None), 'trajectory_id': getattr(session, 'tid', None), 'credential_lease_mode': lease.mode if lease else None, 'credential_writeback': getattr(session, 'credential_writeback', None), 'upstream_transport_mode': getattr(session, 'upstream_transport_mode', 'native-default'), 'result': code, 'cleanup': cleanup, **(audit_extra or {})})
        finally:
            if acquired: app.scheduler.release(rid)
            self.close_connection = True
