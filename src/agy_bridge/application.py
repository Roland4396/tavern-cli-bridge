"""Native account guards, admission, metadata and audit; no HTTP handling."""
import json
import os
import threading
import time
from pathlib import Path
from . import __version__, account_store as accounts
from .core import BridgeError, Scheduler
from .leases import Leases
from .error_detail import redact
from .models import guard_model

MODELS = ['gemini-4-hydrogen-xhigh', 'gemini-4-hydrogen-high', 'gemini-4-hydrogen-medium', 'gemini-4-hydrogen-low', 'gemini-4-argon-high', 'gemini-4-argon-medium', 'gemini-3.8-flash-high', 'gemini-3.8-flash-medium', 'gemini-3.8-flash-low', 'claude-sonnet-4-6', 'claude-opus-4-6-thinking', 'gpt-oss-120b-medium']

class Application:
    def __init__(self, root, store, backend=None, enabled=False, models=None, concurrency=2, max_queue=24, queue_timeout=0, timeout=0, per_account=2, fixed_account=None):
        self.root = Path(root); accounts.private_dir(self.root)
        self.store, self.backend, self.enabled = store, backend, enabled
        self.fixed_account = fixed_account
        self.models = MODELS if models is None else models
        self.scheduler = Scheduler(concurrency, max_queue, per_account); self.leases = Leases(store, per_account, max(360, timeout + 60))
        self.queue_timeout, self.timeout = queue_timeout, timeout
        self.force_cancel = threading.Event(); self.audit_lock = threading.Lock()
        self.state_path = self.root/'account-health.json'
        with accounts.lock(self.root/'health.lock'):
            if not self.state_path.exists(): accounts.atomic_json(self.state_path, {})
    def health(self): return json.loads(self.state_path.read_text())
    def resolve(self, alias=None):
        if self.fixed_account is not None:
            if alias is not None and alias != self.fixed_account:
                raise BridgeError(403, 'account_binding_mismatch', 'This endpoint is bound to one account; no account fallback is permitted')
            alias = self.fixed_account
        try: snap = self.store.snapshot(alias)
        except accounts.AccountError as e: raise BridgeError(503, 'account_unavailable', str(e)) from None
        self.check_account(snap.alias); return snap
    def check_account(self, alias, model=None):
        with accounts.lock(self.store.root/'state.lock', False):
            state = self.store.read(); record = state['accounts'].get(alias, {})
            if not self.store._ready(alias, record): raise BridgeError(503, 'account_unavailable', 'Selected account needs official login')
        local = self.health().get(alias, {})
        last = record.get('last_service_check', {})
        def blocked_error(kind, fallback, **extra):
            prior = local.get('last_public_error') or {}
            allowed = {'upstream_status', 'upstream_code', 'upstream_reason', 'original_message_present', 'message_redacted',
                       'message_truncated', 'reason', 'appeal_url'}
            details = {k:v for k,v in prior.items() if k in allowed and isinstance(v, (str, int, float, bool, type(None)))}
            details.update(extra)
            return BridgeError(403 if kind != 'rate_limited' else 429, kind,
                               redact(prior.get('message'))[0] or fallback,
                               source=prior.get('source', 'bridge_account_guard'), cached=bool(prior),
                               status_source='local_account_guard', observed_at=local.get('checked_at'),
                               dispatch_blocked=True, **details)
        if record.get('official_cli_generation_status') == 'service_disabled_by_server' or local.get('blocked'):
            url = local.get('appeal_url') or last.get('appeal_url')
            raise blocked_error('service_disabled', 'The selected account is disabled by the upstream and is excluded from dispatch.', reason='TOS_VIOLATION', **({'appeal_url': url} if url else {}))
        if local.get('permission_denied'):
            reason = local.get('permission_reason')
            message = 'Official CLI eligibility check rejected this account: service is not available in the current location.' if reason == 'LOCATION_UNAVAILABLE' else 'Selected account requires permission review'
            raise blocked_error('account_permission_denied', message, **({'reason': reason} if reason in ('LOCATION_UNAVAILABLE', 'CLI_INELIGIBLE') else {}))
        if local.get('cooldown_until', 0) > time.time(): raise blocked_error('rate_limited', 'Selected account is cooling down.', retry_after=max(1, int(local['cooldown_until'] - time.time()) + 1))
        model_states = local.get('models', {})
        for name in dict.fromkeys((guard_model(model), model)) if model else ():
            local = model_states.get(name, {})
            if local.get('cooldown_until', 0) > time.time(): raise blocked_error('rate_limited', 'Selected account/model is cooling down.', retry_after=max(1, int(local['cooldown_until'] - time.time()) + 1))
    def outcome(self, alias, error, model=None):
        if error.code not in ('service_disabled', 'rate_limited', 'account_permission_denied'): return
        with accounts.lock(self.root/'health.lock'):
            state = self.health(); rec = state.setdefault(alias, {})
            if error.code == 'rate_limited' and model: rec = rec.setdefault('models', {}).setdefault(guard_model(model), {})
            rec.update({'last_code': error.code, 'checked_at': time.time()})
            allowed = {'message', 'source', 'upstream_status', 'upstream_code', 'upstream_reason', 'original_message_present',
                       'message_redacted', 'message_truncated', 'reason', 'appeal_url'}
            rec['last_public_error'] = {k:v for k,v in error.public()['error'].items() if k in allowed}
            rec['last_public_error']['message'] = redact(error.message)[0]
            if error.code == 'service_disabled': rec.update({'blocked': True, **error.details})
            elif error.code == 'account_permission_denied':
                rec['permission_denied'] = True
                if error.details.get('reason') in ('LOCATION_UNAVAILABLE', 'CLI_INELIGIBLE'):
                    rec['permission_reason'] = error.details['reason']
            elif error.code == 'rate_limited': rec['cooldown_until'] = time.time() + error.details.get('retry_after', 60)
            accounts.atomic_json(self.state_path, state)
    def recover_observed_quota(self, alias, model, query_started_at):
        """Fresh positive full quota may clear only an older exact quota block.

        Never clear permission/TOS, global cooldowns, ordinary throttling, or a
        refusal that arrived while the metadata query was in flight.
        """
        with accounts.lock(self.root/'health.lock'):
            state = self.health(); account = state.get(alias, {})
            if account.get('blocked') or account.get('permission_denied'): return False
            record = account.get('models', {}).get(model, {})
            if (record.get('last_code') != 'rate_limited'
                    or record.get('last_public_error', {}).get('upstream_reason') != 'QUOTA_EXHAUSTED'
                    or record.get('checked_at', 0) > query_started_at
                    or 'cooldown_until' not in record): return False
            record.pop('cooldown_until')
            record['quota_recovered_at'] = time.time()
            record['quota_recovery_source'] = 'official_cli_full_metadata'
            accounts.atomic_json(self.state_path, state)
            return True
    def audit(self, data):
        with self.audit_lock:
            fd = os.open(self.root/'requests.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as f: f.write(json.dumps(data, ensure_ascii=False) + '\n')
    def status(self):
        status = {'version': __version__, 'client_tool_passthrough': True, 'tool_execution': 'caller_only', **self.scheduler.status(), 'native_inference_enabled': self.enabled, 'native_subscription_qualified': False, 'automatic_fallback': False}
        listing = self.store.list(); status['selected_account'] = self.fixed_account or listing['active']; status['fixed_account'] = self.fixed_account; status['accounts'] = []
        for r in listing['accounts']:
            try: self.check_account(r['alias']); code = 'eligible' if self.enabled else 'inference_disabled'
            except BridgeError as e: code = e.code
            status['accounts'].append({'alias': r['alias'], 'authorized_local': r['ready'], 'dispatch_status': code})
        return status

