"""Private operations for Stream's sole QuotaKeeper. No clock or background job.

This token is distinct from the ordinary inference token. Caller prompts, tools,
output overrides and account selection are not accepted on this interface.
"""
import hmac
import time
import uuid
from . import account_store as accounts
from .core import BridgeError, validate
from .execution import serve_native
from .quota import refresh_official

PREFIX = '/internal/quota-keeper/'
KEEPER_MODELS = {**{'gemini-4-argon-' + tier: 'gemini-4-argon-high'
                   for tier in ('low', 'medium', 'high')},
                 **{'gemini-4-hydrogen-' + tier: 'gemini-4-hydrogen-xhigh'
                    for tier in ('low', 'medium', 'high', 'xhigh')}}


class SummaryTransport:
    """Discard generated text; expose only the existing keeper's evidence metrics."""
    def __init__(self, handler):
        self.handler, self.server = handler, handler.server
        self.result = None

    def gone(self):
        return self.handler.gone()

    def reply(self, status, payload, extra=None):
        raw = payload.get('usage') or {}
        usage = {target: raw[source] for source, target in
                 (('prompt_tokens', 'inputTokens'), ('completion_tokens', 'outputTokens'))
                 if type(raw.get(source)) in (int, float)}
        reasoning = (raw.get('completion_tokens_details') or {}).get('reasoning_tokens')
        if type(reasoning) in (int, float):
            usage['reasoningTokens'] = reasoning
        self.result = {'http_status': status, 'ok': status == 200, 'usage': usage,
                       'submitted': (extra or {}).get('X-AGY-Dispatch') != 'not-submitted'}


class KeeperControl:
    def __init__(self, app, config_path, allowed, token):
        self.app, self.config_path = app, config_path
        self.allowed, self.token = frozenset(allowed), token

    def inventory(self):
        rows = []
        for account in self.app.native.store.list()['accounts']:
            alias = account['alias']
            if alias not in self.allowed:
                continue
            expected = (account.get('expected_email') or '').strip().lower()
            observed = (account.get('observed_email') or '').strip().lower()
            if not expected or observed != expected or not account.get('ready'):
                # Never invent a new identity when login metadata is incomplete.
                raise BridgeError(503, 'account_identity_unverified', 'CLI pool identity requires verification')
            try:
                self.app.native.check_account(alias)
                status = 'eligible'
            except BridgeError as exc:
                status = exc.code
            rows.append({'account': alias, 'email': observed, 'status': status})
        return {'accounts': rows, 'controller': 'stream_quota_keeper', 'owns_schedule': False}

    def reserve(self, alias, key):
        app = self.app
        with app.cv:
            if not app.enabled or not app.native.enabled or app.draining:
                raise BridgeError(503, 'draining', 'CLI control is not accepting work')
            if app.waiting or len(app.running) >= app.limit or alias in app.running_accounts.values():
                raise BridgeError(409, 'account_busy', 'Foreground work has priority')
            app.native.resolve(alias)
            app.running[key] = 'cli_keeper'
            app.running_accounts[key] = alias

    def handle(self, handler):
        if not self.token or not hmac.compare_digest(
                handler.headers.get('Authorization', ''), 'Bearer ' + self.token):
            return handler.reply(401, BridgeError(401, 'unauthorized', 'Unauthorized').public())
        key = None
        try:
            if handler.command == 'GET' and handler.path == PREFIX + 'accounts':
                return handler.reply(200, self.inventory())
            if handler.command != 'POST' or handler.path not in (PREFIX + 'quota', PREFIX + 'generate'):
                raise BridgeError(404, 'not_found', 'Not found')
            body = handler.read_json()
            generation = handler.path.endswith('/generate')
            fields = {'account', 'model'} if generation else {'account'}
            if set(body) != fields or not isinstance(body.get('account'), str) or body['account'] not in self.allowed:
                raise BridgeError(400, 'invalid_control_request', 'Expected an exact configured account')
            alias = body['account']
            if generation and (not isinstance(body.get('model'), str) or body['model'] not in KEEPER_MODELS):
                raise BridgeError(400, 'invalid_control_model', 'Only configured tiny keeper models are accepted')
            key = 'keeper-' + uuid.uuid4().hex
            self.reserve(alias, key)
            def may_refresh(_alias):
                with self.app.cv:
                    return not (self.app.draining or self.app.waiting or handler.gone())
            if not generation:
                result = refresh_official(self.config_path, [alias], can_refresh=may_refresh, diagnostic=True)[0]
                return handler.reply(200, result)
            # Check both the family guard and exact low-tier guard. A model
            # alias must not bypass an exhausted/permission-rejected family.
            model = body['model']
            self.app.native.check_account(alias, KEEPER_MODELS[model])
            standing = self.app.native.backend.quota_store.standing(alias, KEEPER_MODELS[model])
            if standing['blocked_until'] and standing['blocked_until'] > time.time():
                raise BridgeError(429, 'quota_exhausted', 'Observed family quota is exhausted')
            req = validate({'model': model, 'messages': [{'role': 'user', 'content': 'ping'}],
                            'max_tokens': 16, 'stream': False}, set(KEEPER_MODELS))
            transport = SummaryTransport(handler)
            serve_native(transport, self.app.native, req, snapshot=self.app.native.resolve(alias),
                         audit_extra={'purpose': 'stream_quota_keeper', 'automatic_retry': False})
            return handler.reply(200, transport.result or {'http_status': 0, 'ok': False, 'usage': {}})
        except BridgeError as exc:
            return handler.reply(exc.status, exc.public(), {'X-AGY-Dispatch': 'not-submitted'})
        except (OSError, ValueError, TypeError, accounts.AccountError):
            return handler.reply(503, BridgeError(503, 'control_unavailable', 'CLI control is unavailable').public())
        finally:
            if key:
                self.app.release(key)
