"""Server-owned permitted account pools; one pinned selection before dispatch.
Earliest-reset ordering follows the deployed Gproxy selector: future observed
reset first, equal/all-unknown resets round-robin; no headroom weighting.
"""
from dataclasses import dataclass
from types import MappingProxyType
import time
import json
from . import account_store as accounts
from .core import BridgeError
from .quota import QuotaStore, CATALOG_ROUTABLE
from .models import EXPLICIT_NATIVE

@dataclass(frozen=True)
class AccountPlan:
    alias: str
    snapshot: object = None
    error: object = None
    model: str = ''
    signature: str = ''
    reset_at: object = None
    candidates: tuple = ()

class AccountRouter:
    def __init__(self, native, *, allowed, default, model_accounts=None, models=(), strategy='fixed', catalog_models=()):
        if not isinstance(allowed, (list, tuple)) or not allowed or len(allowed) > 64:
            raise ValueError('native_accounts must be a nonempty list of at most 64 aliases')
        if any(not isinstance(a, str) or not accounts.ALIAS.fullmatch(a) for a in allowed) or len(set(allowed)) != len(allowed):
            raise ValueError('native_accounts must contain unique valid account aliases')
        if default not in allowed: raise ValueError('native_default_account must be in native_accounts')
        if strategy not in ('fixed', 'earliest_reset'): raise ValueError('Unknown native account strategy')
        routes = {} if model_accounts is None else model_accounts
        if not isinstance(routes, dict) or any(model not in models for model in routes): raise ValueError('Invalid native model pool')
        normalized = {}
        for model, value in routes.items():
            pool = [value] if isinstance(value, str) else value
            if not isinstance(pool, list) or not pool or len(set(pool)) != len(pool) or any(a not in allowed for a in pool):
                raise ValueError('Model pools must contain unique allowed accounts')
            if strategy == 'fixed' and len(pool) != 1: raise ValueError('Fixed strategy needs one account per model')
            normalized[model] = tuple(pool)
        if (not isinstance(catalog_models, (tuple, list)) or not set(catalog_models) <= CATALOG_ROUTABLE & set(models)
                or set(catalog_models) & set(normalized) or (catalog_models and strategy != 'earliest_reset')):
            raise ValueError('Invalid dynamic catalogue binding')
        self.catalog_models = frozenset(catalog_models)
        self.native = native; self.allowed = tuple(allowed); self.default = default
        self.strategy = strategy; self.models = tuple(sorted(models)); self.model_accounts = MappingProxyType(normalized)
        self.quotas = QuotaStore(native.root)
        self.counter_path = native.root/'account-selection.json'; self.counter_lock = native.root/'account-selection.lock'
    @classmethod
    def from_config(cls, native, cfg, models):
        legacy = cfg.get('account'); allowed = cfg.get('native_accounts', [legacy] if legacy else [])
        return cls(native, allowed=allowed, default=cfg.get('native_default_account', legacy),
                   model_accounts=cfg.get('native_model_accounts'), models=models,
                   strategy=cfg.get('native_account_strategy', 'fixed'), catalog_models=cfg.get('native_catalog_models', ()))
    def pool_for(self, model, *, state=None):
        if model in self.catalog_models:
            snapshots = (self.quotas.read() if state is None else state).get('enabled_catalogs', {})
            return tuple(a for a in self.allowed if model in snapshots.get(a, {}).get('models', ()))
        if model in EXPLICIT_NATIVE: return self.model_accounts.get(model, ())
        return self.model_accounts.get(model, (self.default,) if self.strategy == 'fixed' else self.allowed)
    def counters(self):
        return json.loads(self.counter_path.read_text()) if self.counter_path.exists() else {}
    def plan(self, model, reserved=None):
        reserved = reserved or {}; eligible = []; errors = []; busy = False
        try: quota_state = self.quotas.read()
        except (OSError, ValueError):
            if model in self.catalog_models:
                return AccountPlan('', error=BridgeError(503, 'cli_unavailable', 'Native catalogue store unavailable'), model=model)
            quota_state = {}
        now = time.time()
        pool = self.pool_for(model, state=quota_state)
        if model in self.catalog_models and not pool:
            return AccountPlan('', error=BridgeError(503, 'no_catalog_candidate', 'No enabled native account catalogue advertises this model'), model=model)
        for alias in pool:
            try:
                snap = self.native.resolve(alias); self.native.check_account(alias, model)
                standing = self.quotas.standing(alias, model, quota_state, now)
                if standing.get('blocked_without_reset'):
                    raise BridgeError(429, 'quota_exhausted', 'Provider quota is unavailable until refreshed', dispatch_blocked=True)
                if standing['blocked_until'] is not None:
                    raise BridgeError(429, 'quota_exhausted', 'All applicable quota must be available before dispatch',
                                      retry_after=max(1, int(standing['blocked_until']-now)+1), dispatch_blocked=True)
                if reserved.get(alias, 0) >= self.native.leases.parallel_capacity(alias) or self.native.leases.is_busy(alias):
                    busy = True; continue
                eligible.append((alias, snap, standing['reset_at']))
            except BridgeError as exc: errors.append(exc)
            except (OSError, accounts.AccountError): errors.append(BridgeError(503, 'cli_unavailable', 'CLI account store is unavailable'))
        if not eligible:
            error = BridgeError(503, 'accounts_busy', 'Eligible CLI accounts are busy') if busy else (errors[0] if errors else BridgeError(503, 'account_unavailable', 'No eligible CLI account'))
            return AccountPlan('', error=error, model=model)
        if self.strategy == 'earliest_reset':
            known = [r[2] for r in eligible if r[2] is not None]
            if known: eligible = [r for r in eligible if r[2] == min(known)]
        eligible.sort(key=lambda r: r[0]); signature = ','.join(r[0] for r in eligible)
        index = 0
        if self.strategy == 'earliest_reset':
            with accounts.lock(self.counter_lock, False):
                counter = self.counters().get(signature, {})
                if counter.get('at', 0)+7*86400 > now: index = counter.get('count', 0) % len(eligible)
        alias, snap, reset = eligible[index]
        return AccountPlan(alias, snap, model=model, signature=signature, reset_at=reset, candidates=tuple(r[0] for r in eligible))
    def commit(self, plan):
        if self.strategy != 'earliest_reset': return
        with accounts.lock(self.counter_lock):
            now = time.time(); state = {k:v for k,v in self.counters().items() if v.get('at', 0)+7*86400 > now}
            counter = state.get(plan.signature, {}).get('count', 0)
            state[plan.signature] = {'count': counter+1, 'at': now}
            accounts.atomic_json(self.counter_path, state)
    def status(self):
        rows = []
        try: quota_state = self.quotas.read()
        except (OSError, ValueError): quota_state = {}
        for alias in self.allowed:
            try:
                self.native.resolve(alias); state = 'ready' if self.native.enabled else 'inference_disabled'
                row = {'alias': alias, 'registered': True, 'authorized_local': True, 'dispatch_status': state}
            except BridgeError as exc:
                with accounts.lock(self.native.store.root/'state.lock', False):
                    record = self.native.store.read()['accounts'].get(alias)
                    ready = bool(record and self.native.store._ready(alias, record))
                row = {'alias': alias, 'registered': record is not None, 'authorized_local': ready, 'dispatch_status': exc.code}
            except (OSError, accounts.AccountError):
                row = {'alias': alias, 'registered': None, 'authorized_local': False, 'dispatch_status': 'cli_unavailable'}
            row['quota'] = {m:self.quotas.standing(alias, m, quota_state) for m in self.models}
            row['model_dispatch_status'] = {}
            for model in self.models:
                try:
                    self.native.check_account(alias, model)
                    if model in self.catalog_models and alias not in self.pool_for(model, state=quota_state):
                        row['model_dispatch_status'][model] = 'not_in_enabled_catalog'
                        continue
                    state = 'quota_exhausted' if row['quota'][model]['blocked_until'] is not None or row['quota'][model].get('blocked_without_reset') else 'eligible'
                except BridgeError as exc: state = exc.code
                except (OSError, accounts.AccountError): state = 'cli_unavailable'
                row['model_dispatch_status'][model] = state
            rows.append(row)
        routes = {k:(v[0] if self.strategy == 'fixed' else list(v)) for k,v in self.model_accounts.items()}
        routes.update({m:list(self.pool_for(m,state=quota_state)) for m in self.catalog_models})
        return {'catalog_routed_models': sorted(self.catalog_models), 'default_account': self.default if self.strategy == 'fixed' else None, 'model_accounts': routes, 'strategy': self.strategy,
                'configured_accounts': rows, 'selection': 'server_owned_at_admission',
                'automatic_account_selection': self.strategy != 'fixed', 'automatic_account_rotation': self.strategy != 'fixed',
                'account_switch_after_dispatch': False,
                'retry_after_dispatch': False}
