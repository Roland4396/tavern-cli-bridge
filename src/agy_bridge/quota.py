"""Observed CLI quota windows. Scheduling/status never make quota requests."""
import argparse
import datetime
import json
import math
import tempfile
import time
from pathlib import Path
from . import account_store as accounts
from .models import CARBON

MODELS = ('gemini-4-argon-high', 'gemini-4-hydrogen-xhigh')
BUCKET_MODELS = {
    'gemini-argon-wtu-weekly': ('gemini-4-argon-high',),
    'gemini-weekly': ('gemini-4-hydrogen-xhigh',),
    'gemini-5h': ('gemini-4-hydrogen-xhigh',),
}
MODEL_BUCKETS = {model: tuple(key for key, models in BUCKET_MODELS.items() if model in models)
                 for model in MODELS}

def observation(info, now):
    if not isinstance(info, dict): return None
    try:
        date = datetime.datetime.fromisoformat(info['resetTime'].replace('Z', '+00:00'))
        remaining = float(info['remainingFraction'])
        if date.tzinfo is None or not math.isfinite(remaining) or not 0 <= remaining <= 1: return None
        return {'reset_at': date.timestamp(), 'remaining_fraction': remaining, 'observed_at': now}
    except (KeyError, TypeError, ValueError, OverflowError): return None

class QuotaStore:
    def __init__(self, root):
        self.root = Path(root); accounts.private_dir(self.root)
        self.path = self.root/'quota-observations.json'
        self.lock = self.root/'quota-observations.lock'
    def read(self):
        if not self.path.exists(): return {'version': 1, 'accounts': {}}
        return json.loads(self.path.read_text())
    def merge(self, alias, updates):
        if not updates: return
        with accounts.lock(self.lock):
            state = self.read(); models = state['accounts'].setdefault(alias, {})
            for model, windows in updates.items():
                rows = models.setdefault(model, {})
                for key, item in windows.items():
                    old = rows.get(key, {})
                    if old.get('observed_at', 0) <= item['observed_at']: rows[key] = item
                    # Keep the real bucket and catalogue readings distinct. A
                    # cached catalogue must not erase a fresh exhausted bucket.
            accounts.atomic_json(self.path, state)
    def observe_catalog(self, alias, data):
        now = time.time(); updates = {}
        for model in data.get('clientModelConfigs', []):
            name = model.get('modelId')
            item = observation(model.get('quotaInfo'), now)
            if (name in MODELS or name in CARBON) and item: updates[name] = {'catalog': item}
        self.merge(alias, updates)
        return updates
    def observe_summary(self, alias, data):
        now = time.time(); updates = {}
        for group in data.get('response', {}).get('groups', []):
            for bucket in group.get('buckets', []):
                key = bucket.get('bucketId'); item = observation(bucket, now)
                if item:
                    for model in BUCKET_MODELS.get(key, ()): updates.setdefault(model, {})[key] = item
        self.merge(alias, updates)
        return updates
    def standing(self, alias, model, state=None, now=None):
        if model in ('gemini-4-argon-low', 'gemini-4-argon-medium'):
            model = 'gemini-4-argon-high'
        elif model in ('gemini-4-hydrogen-low', 'gemini-4-hydrogen-medium', 'gemini-4-hydrogen-high'):
            model = 'gemini-4-hydrogen-xhigh'
        now = time.time() if now is None else now
        state = self.read() if state is None else state
        windows = state.get('accounts', {}).get(alias, {}).get(model, {})
        bucket_keys = MODEL_BUCKETS.get(model, ())
        if model in CARBON:
            # Both Gemini quota windows apply on this account. Keep exact-tier
            # catalogue signals separate; never infer quota recovery from a sibling.
            bucket_keys = ('gemini-weekly', 'gemini-5h')
            shared = state.get('accounts', {}).get(alias, {}).get('gemini-4-hydrogen-xhigh', {})
            windows = {**windows, **{k:shared[k] for k in bucket_keys if k in shared}}
        buckets = [windows.get(key) for key in bucket_keys]
        complete_at = min((v.get('observed_at', 0) for v in buckets), default=0) if buckets and all(buckets) else 0
        # A complete newer summary replaces an older catalogue observation,
        # including when the provider moved a reset boundary. A newer zero in
        # the catalogue still blocks, but a positive catalogue alone cannot
        # clear a previously exhausted authoritative bucket.
        future = [v for key, v in windows.items() if v.get('reset_at', 0) > now
                  and not (key == 'catalog' and v.get('observed_at', 0) < complete_at)]
        exhausted = [v['reset_at'] for v in future if v.get('remaining_fraction', 1) <= 0]
        return {'reset_at': min((v['reset_at'] for v in future), default=None),
                'blocked_until': max(exhausted, default=None),
                'observed_at': max((v.get('observed_at', 0) for v in windows.values()), default=None)}

def quota_diagnostic(summary, catalog, observed_at):
    """Lean normalized metadata, no tokens, labels, experiments or response text."""
    buckets = []
    for group in summary.get('response', {}).get('groups', []):
        for bucket in group.get('buckets', []):
            if bucket.get('bucketId') in BUCKET_MODELS and observation(bucket, observed_at):
                buckets.append({key: bucket[key] for key in
                    ('bucketId', 'remainingFraction', 'resetTime', 'disabled') if key in bucket})
    models = sorted({item['modelId'] for item in catalog.get('clientModelConfigs', [])
                     if isinstance(item, dict) and isinstance(item.get('modelId'), str)
                     and item.get('disabled') is not True})
    return {'observed_at_ms': int(observed_at * 1000), 'models': models, 'buckets': buckets}

def refresh_official(config, aliases, *, can_refresh=None, diagnostic=False):
    """Explicit admin metadata read, inside the container; cannot submit a turn."""
    from .native import NativeBackend, Session
    from .application import Application
    from .core import BridgeError
    cfg = json.loads(Path(config).read_text()); store = accounts.Accounts(cfg['accounts_root'])
    root = Path(cfg['root'])/'native'; quotas = QuotaStore(root)
    allowed = cfg.get('native_accounts', [])
    results = []
    class MetadataReady(Exception): pass
    class MetadataSession(Session):
        def post(self, name, body, proto=False, **kwargs):
            if name == 'GetCascadeTrajectory': raise MetadataReady()
            raise RuntimeError('Metadata-only session rejected non-quota RPC: '+name)
    for alias in aliases:
        if alias not in allowed: raise ValueError('Account is outside the configured CLI pool')
        result = {'account': alias, 'generation_requests': 0, 'method': 'official_cli_metadata_only'}
        session = None; app = None
        started_at = time.time()
        deadline = time.monotonic()+45
        def check():
            if can_refresh is not None and not can_refresh(alias):
                raise BridgeError(499, 'quota_refresh_deferred', 'Quota refresh yielded to foreground work or shutdown')
            if time.monotonic() > deadline:
                # Session.post's cancellation watcher handles BridgeError and
                # interrupts a blocked metadata socket. This is not a model
                # generation deadline.
                raise BridgeError(504, 'quota_refresh_timeout', 'Quota metadata read deadline')
        try:
            check()
            with accounts.lock(store.root/'profiles'/alias/'run.lock', wait=False):
                check()
                with tempfile.TemporaryDirectory(prefix='agy-quota-', dir='/tmp') as temp:
                    backend = NativeBackend(temp, store)
                    app = Application(root, store, backend, enabled=False)
                    snap = app.resolve(alias)  # Preserve existing permission/ban guards.
                    session = MetadataSession(backend, {'model': MODELS[0], 'messages': [{'role': 'user', 'content': ''}]}, snap, check)
                    try:
                        try: session.start()
                        except MetadataReady: pass
                        assert session.port and not session.started
                        summary = json.loads(Session.post(session, 'RetrieveUserQuotaSummary', {}))
                        summary_updates = quotas.observe_summary(alias, summary)
                        catalog = json.loads(Session.post(session, 'GetCascadeModelConfigData', {}))
                        catalog_updates = quotas.observe_catalog(alias, catalog)
                        complete = {model: all(key in summary_updates.get(model, {}) for key in MODEL_BUCKETS[model])
                                    for model in MODELS}
                        result['ok'] = all(complete.values())
                        result['status'] = 'ok' if result['ok'] else 'partial'
                        result['complete_summary'] = complete
                        recovered = []
                        now = time.time()
                        for model in MODELS:
                            current = list(summary_updates.get(model, {}).values())
                            observed_catalog = catalog_updates.get(model, {}).get('catalog')
                            if complete[model] and observed_catalog and all(
                                    item['reset_at'] > now and item['remaining_fraction'] > 0
                                    for item in [*current, observed_catalog]):
                                if app.recover_observed_quota(alias, model, started_at): recovered.append(model)
                        result['recovered_models'] = recovered
                        if diagnostic:
                            result['diagnostic'] = quota_diagnostic(summary, catalog, time.time())
                        result['models'] = {model: quotas.standing(alias, model) for model in MODELS}
                    finally: session.close()
        except (OSError, ValueError, TypeError, RuntimeError, accounts.AccountError, BridgeError) as exc:
            if app and isinstance(exc, BridgeError) and exc.code in ('service_disabled', 'account_permission_denied'):
                app.outcome(alias, exc)
            busy = isinstance(exc, accounts.AccountError) and str(exc).startswith('Account is busy;')
            deferred = isinstance(exc, BridgeError) and exc.code == 'quota_refresh_deferred'
            result.update(ok=False, status='busy' if busy or deferred else 'error',
                          error='account_busy' if busy else exc.code if isinstance(exc, BridgeError) else type(exc).__name__)
        finally:
            result['worker_stopped'] = session is None or session.proc is None or session.proc.poll() is not None
        results.append(result)
    return results

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='/config/bridge.json'); p.add_argument('--account', action='append', required=True)
    args = p.parse_args(); results = refresh_official(args.config, args.account)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(r.get('ok') for r in results) else 1

if __name__ == '__main__': raise SystemExit(main())
