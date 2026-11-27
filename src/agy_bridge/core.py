"""Text/function request contract, safe errors and bounded explicit-account scheduling."""
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from .error_detail import original_message, redact

class BridgeError(Exception):
    def __init__(self, status, code, message, **details):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details
    def public(self):
        return {'error': {'message': self.message, 'type': self.code, 'code': self.code, 'status': self.status, 'source': 'bridge', **self.details}}

IGNORED = frozenset('temperature top_p top_k min_p top_a typical_p tfs tail_free_sampling presence_penalty frequency_penalty repetition_penalty repetition_penalty_range repetition_penalty_slope seed logit_bias logprobs top_logprobs reasoning_effort verbosity user metadata store service_tier cache_control include_reasoning return_prompt'.split())
SUPPORTED = {'model', 'messages', 'stream', 'stream_options', 'max_tokens', 'max_completion_tokens', 'stop', 'n', 'tools', 'tool_choice', 'response_format', 'parallel_tool_calls'}

@dataclass(frozen=True)
class Request:
    body: dict
    ignored: tuple
    stops: tuple
    include_usage: bool


def validate(body, models):
    def bad(message): raise BridgeError(400, 'invalid_request', message)
    if not isinstance(body, dict): bad('Expected a JSON object')
    unknown = set(body) - SUPPORTED - IGNORED
    if unknown: bad('Unsupported fields: ' + ', '.join(sorted(unknown)))
    if body.get('model') not in models: bad('Unknown or unconfigured model')
    messages = body.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 4096: bad('messages must contain 1–4096 entries')
    normalized, dialog, total, known_calls, pending = [], False, 0, {}, set()
    for message in messages:
        if not isinstance(message, dict) or set(message) - {'role', 'content', 'name', 'tool_calls', 'tool_call_id'}: bad('Unsupported message fields')
        role = message.get('role'); content = message.get('content')
        if role not in ('system', 'user', 'assistant', 'tool'): bad('Unsupported message role')
        if message.get('name'): bad('Named speakers are not supported')
        if content is None and role == 'assistant' and message.get('tool_calls'): content = ''
        if isinstance(content, list):
            if not all(isinstance(p, dict) and set(p) == {'type', 'text'} and p['type'] == 'text' and isinstance(p['text'], str) for p in content): bad('Only text content parts are supported')
            content = ''.join(p['text'] for p in content)
        if not isinstance(content, str): bad('Message content must be text')
        # Native transport collects all system messages in source order.
        # Tavern may insert them between dialogue turns; do not reject them.
        if pending and role != 'tool': bad('All pending tool calls need results before continuing')
        dialog = dialog or role != 'system'
        total += len(content.encode('utf-8'))
        item = {'role': role, 'content': content}
        if role == 'assistant' and message.get('tool_calls'):
            calls = message['tool_calls']
            if not isinstance(calls, list) or len(calls) > 128: bad('Invalid assistant tool_calls')
            normalized_calls = []
            for call in calls:
                if not isinstance(call, dict) or set(call) != {'id', 'type', 'function'} or call.get('type') != 'function': bad('Only function tool calls are supported')
                cid = call.get('id'); fn = call.get('function')
                if not isinstance(cid, str) or not cid or len(cid) > 256 or cid in known_calls: bad('Invalid or duplicate tool call ID')
                if not isinstance(fn, dict) or set(fn) != {'name', 'arguments'} or not isinstance(fn.get('name'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', fn['name']) or not isinstance(fn.get('arguments'), str): bad('Invalid tool call function')
                try:
                    args = json.loads(fn['arguments'])
                    if not isinstance(args, dict): bad('Historical tool arguments must be a JSON object')
                except ValueError: bad('Historical tool arguments must be valid JSON')
                normalized_calls.append({'id': cid, 'type': 'function', 'function': dict(fn)})
                known_calls[cid] = normalized_calls[-1]; pending.add(cid); total += len(fn['arguments'].encode())
            item['tool_calls'] = normalized_calls
        elif message.get('tool_calls'): bad('Only assistant messages may contain tool_calls')
        if role == 'tool':
            cid = message.get('tool_call_id')
            if not isinstance(cid, str) or cid not in pending: bad('Tool result must reference one unresolved tool call')
            pending.remove(cid); item['tool_call_id'] = cid
        elif 'tool_call_id' in message: bad('tool_call_id is only valid on tool messages')
        normalized.append(item)
    if total > 2500000: raise BridgeError(413, 'input_too_large', 'Input exceeds the locally validated text bound')
    if pending: bad('Tool results are missing for pending calls')
    if normalized[-1]['role'] not in ('user', 'tool'): bad('The last message must be user or tool')
    if type(body.get('stream', False)) is not bool: bad('stream must be boolean')
    limits = [body[k] for k in ('max_tokens', 'max_completion_tokens') if body.get(k) is not None]
    if len(limits) == 2 and limits[0] != limits[1]: bad('Conflicting output limits')
    limit = limits[0] if limits else 4096
    if type(limit) is not int or limit < 1: bad('Output limit must be a positive integer')
    if body.get('n', 1) != 1 or isinstance(body.get('n', 1), bool): bad('Only n=1 is supported')
    tools = body.get('tools')
    if tools is None: tools = []
    if not isinstance(tools, list) or len(tools) > 128: bad('tools must contain at most 128 function definitions')
    names, normalized_tools = set(), []
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {'type', 'function'} or tool['type'] != 'function': bad('Only function tools are supported')
        fn = tool['function']
        if not isinstance(fn, dict) or set(fn) - {'name', 'description', 'parameters', 'strict'}: bad('Unsupported function definition')
        name = fn.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) or name in names: bad('Invalid or duplicate function name')
        names.add(name)
        if not isinstance(fn.get('description', ''), str): bad('Function description must be text')
        schema = fn.get('parameters', {'type': 'object', 'properties': {}})
        if not isinstance(schema, dict) or schema.get('type') != 'object': bad('Function parameters must be an object JSON schema')
        if fn.get('strict') not in (None, False): raise BridgeError(501, 'unsupported_tool_option', 'strict=true is not yet qualified by the native CLI route')
        normalized_tools.append({'type': 'function', 'function': {'name': name, 'description': fn.get('description', ''), 'parameters': schema}})
    choice = body.get('tool_choice', 'auto')
    if choice is None: choice = 'auto'
    if isinstance(choice, str):
        if choice not in ('auto', 'none', 'required'): bad('Invalid tool_choice mode')
        if choice == 'required' and not normalized_tools: bad('tool_choice required needs at least one function')
    elif isinstance(choice, dict):
        fn = choice.get('function')
        if set(choice) != {'type', 'function'} or choice.get('type') != 'function' or not isinstance(fn, dict) or set(fn) != {'name'} or not isinstance(fn.get('name'), str) or fn['name'] not in names:
            bad('Named tool_choice must select one declared function')
        choice = {'type': 'function', 'function': {'name': fn['name']}}
    else: bad('Invalid tool_choice')
    parallel = body.get('parallel_tool_calls', True)
    if type(parallel) is not bool: bad('parallel_tool_calls must be boolean')
    if not parallel: raise BridgeError(501, 'unsupported_tool_option', 'parallel_tool_calls=false is not yet qualified')
    if body.get('response_format') not in (None, {'type': 'text'}): bad('Structured output is not supported')
    opts = body.get('stream_options') or {}
    if not isinstance(opts, dict) or set(opts) - {'include_usage'} or type(opts.get('include_usage', False)) is not bool: bad('Unsupported stream_options')
    stops = body.get('stop') or []
    if isinstance(stops, str): stops = [stops]
    if not isinstance(stops, list) or len(stops) > 16 or any(not isinstance(s, str) or not s or len(s) > 256 for s in stops): bad('stop must contain up to 16 nonempty strings of at most 256 characters')
    return Request({'model': body['model'], 'messages': normalized, 'stream': body.get('stream', False), 'max_tokens': limit, 'tools': normalized_tools, 'tool_choice': choice}, tuple(sorted(set(body) & IGNORED)), tuple(stops), opts.get('include_usage', False))

def native_tool_choice_supported(body):
    return body.get('tool_choice') != 'required' or len(body.get('tools', [])) == 1


def forced_tool_name(body):
    choice = body.get('tool_choice', 'auto')
    if isinstance(choice, dict): return choice['function']['name']
    if choice == 'required' and len(body.get('tools', [])) == 1:
        return body['tools'][0]['function']['name']
    return None


def exposed_tools(body):
    return [] if body.get('tool_choice') == 'none' else body.get('tools', [])


def allowed_tool_names(body):
    forced = forced_tool_name(body)
    return {forced} if forced else {t['function']['name'] for t in exposed_tools(body)}


class StopFilter:
    """Hold only possible stop prefixes, including across chunk boundaries."""
    def __init__(self, stops): self.stops, self.pending, self.hit = stops, '', False
    def feed(self, text, final=False):
        if self.hit: return ''
        self.pending += text
        positions = [self.pending.find(s) for s in self.stops if s in self.pending]
        if positions:
            output = self.pending[:min(positions)]; self.pending = ''; self.hit = True; return output
        hold = 0
        if not final:
            for s in self.stops:
                for n in range(1, min(len(s), len(self.pending) + 1)):
                    if self.pending.endswith(s[:n]): hold = max(hold, n)
        if hold: output, self.pending = self.pending[:-hold], self.pending[-hold:]
        else: output, self.pending = self.pending, ''
        return output


def upstream_error(raw, secrets=()):
    """Pass through the readable error; normalize classification, redact secrets."""
    encoded = json.dumps(raw, ensure_ascii=False) if not isinstance(raw, str) else raw
    stack, nodes = [raw], []
    while stack and len(nodes) < 256:
        item = stack.pop()
        if isinstance(item, dict): nodes.append(item); stack.extend(item.values())
        elif isinstance(item, list): stack.extend(item)
        elif isinstance(item, str):
            # CLI sometimes embeds a complete Google error JSON in a message.
            for match in list(re.finditer(r'\{\s*"(?:error|code)"', item))[:8]:
                try: value, _ = json.JSONDecoder().raw_decode(item[match.start():]); stack.append(value)
                except ValueError: pass
    code, reason, appeal, retry = None, None, None, None
    for n in nodes:
        c = n.get('code', n.get('errorCode'))
        if type(c) is int and 400 <= c <= 599: code = c
        if n.get('reason') == 'TOS_VIOLATION': reason = 'TOS_VIOLATION'
        url = n.get('appeal_url')
        if isinstance(url, str) and len(url) < 2048:
            u = urlsplit(url)
            if u.scheme == 'https' and u.hostname in ('forms.gle', 'support.google.com', 'docs.google.com') and not u.username: appeal = url
        r = n.get('retry_after', n.get('retryDelay'))
        try:
            r = float(str(r).rstrip('s'))
            if math.isfinite(r): retry = min(86400, max(1, math.ceil(r)))
        except (ValueError, TypeError): pass
    # Native Google SDK errors also use Go's Details: [map[...]] rendering.
    if code is None:
        match = re.search(r'Error (\d{3}),', encoded)
        if match: code = int(match[1])
    if 'TOS_VIOLATION' in encoded: reason = 'TOS_VIOLATION'
    if appeal is None:
        match = re.search(r'appeal_url[: ]+(https://[^\s\[\]]+)', encoded)
        if match:
            u = urlsplit(match[1])
            if u.hostname in ('forms.gle', 'support.google.com', 'docs.google.com') and not u.username: appeal = match[1]
    message, redacted, truncated = redact(original_message(raw, nodes), secrets)
    symbols = [n.get(k) for n in nodes for k in ('status','code')
               if isinstance(n.get(k),str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{1,80}',n[k])
               and not n[k].startswith('CORTEX_')]
    match = re.search(r', Status: ([A-Z][A-Z0-9_]+)', encoded)
    symbolic = symbols[-1] if symbols else match[1] if match else None
    native_reason = next((n['reason'] for n in reversed(nodes) if isinstance(n.get('reason'),str)
                          and re.fullmatch(r'[A-Z][A-Z0-9_]{1,80}',n['reason'])), None)
    def result(status, kind, fallback, **details):
        return BridgeError(status, kind, message or fallback, source='antigravity_cli',
                           upstream_status=code, status_source='upstream_http' if code else 'bridge_classification',
                           original_message_present=bool(message), message_redacted=redacted,
                           message_truncated=truncated, **({'upstream_code': symbolic} if symbolic else {}), **({'upstream_reason': native_reason} if native_reason else {}), **details)
    if reason or 'service has been disabled' in encoded.lower():
        return result(403, 'service_disabled', 'The upstream disabled this account; it will not be dispatched again.', **({'appeal_url': redact(appeal, secrets)[0]} if appeal else {}), reason='TOS_VIOLATION')
    if 'Eligibility check failed:' in encoded and (code in (502, 503, 504) or (code is None and message.rstrip().endswith(': Bad Gateway'))):
        return result(code or 502, 'cli_transport_error', 'CLI initialization transport failed; this is not an account eligibility rejection.')
    eligibility_check = 'Eligibility check failed:' in encoded
    location = 'not currently available in your location' in encoded
    # A credential rejection applies to this request. It is not evidence of
    # account ineligibility and must not create a durable permission flag.
    if (code == 401 or (code is None and symbolic == 'UNAUTHENTICATED')) and not (
            eligibility_check and (location or native_reason in ('LOCATION_UNAVAILABLE', 'CLI_INELIGIBLE'))):
        return result(401, 'upstream_unauthenticated', 'The upstream did not accept the authentication credential for this request.')
    if eligibility_check:
        return result(403, 'account_permission_denied', 'Official CLI eligibility check rejected this account.',
                      reason='LOCATION_UNAVAILABLE' if location else 'CLI_INELIGIBLE')
    if code == 429 or (code is None and 'RESOURCE_EXHAUSTED' in encoded.upper()):
        return result(429, 'rate_limited', 'The selected account is rate limited; no automatic retry or account fallback.', retry_after=retry or 60)
    if code == 403 or (code is None and 'PERMISSION_DENIED' in encoded.upper()):
        return result(code or 403, 'account_permission_denied', 'The selected account needs authorization or permission review.')
    return result(code or 502, 'upstream_error', 'The CLI upstream request failed.')

class Scheduler:
    """FIFO among eligible accounts; immutable routing, no retries/fallback."""
    def __init__(self, concurrency=2, max_queue=24, per_account=2):
        if per_account not in (1, 2): raise ValueError("Account concurrency must be 1 or 2")
        self.limit, self.max_queue, self.per_account = concurrency, max_queue, per_account
        self.cv = threading.Condition(); self.waiting = []; self.running = {}; self.draining = False
    def acquire(self, key, alias, check, try_lease):
        entry = (key, alias)
        with self.cv:
            if self.draining: raise BridgeError(503, 'draining', 'Server is draining')
            if len(self.waiting) >= self.max_queue: raise BridgeError(429, 'queue_full', 'Request queue is full', retry_after=1)
            self.waiting.append(entry)
            try:
                while True:
                    check()
                    if self.draining: raise BridgeError(503, 'draining', 'Server is draining')
                    counts = {a: list(self.running.values()).count(a) for a in self.running.values()}
                    if len(self.running) < self.limit and counts.get(alias, 0) < self.per_account:
                        # Preserve eligible FIFO; bypass only externally blocked accounts.
                        earlier = self.waiting[:self.waiting.index(entry)]
                        first_waits = any(counts.get(e[1], 0) < self.per_account and not try_lease.is_busy(e[1]) for e in earlier)
                        if not first_waits:
                            lease = try_lease(alias)
                            if lease is not None:
                                self.waiting.remove(entry); self.running[key] = alias; self.cv.notify_all(); return lease
                    self.cv.wait(.05)
            except BaseException:
                if entry in self.waiting: self.waiting.remove(entry)
                self.cv.notify_all(); raise
    def release(self, key):
        with self.cv: self.running.pop(key, None); self.cv.notify_all()
    def drain(self):
        with self.cv: self.draining = True; self.cv.notify_all()
    def status(self):
        with self.cv: return {'active': len(self.running), 'queued': len(self.waiting), 'draining': self.draining, 'global_limit': self.limit, 'per_account_limit': self.per_account, 'active_by_account': {a: list(self.running.values()).count(a) for a in set(self.running.values())}}
