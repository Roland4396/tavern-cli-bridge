"""Server-owned admission policy; never replays a selected backend."""
import copy
import ipaddress
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from . import __version__, account_store as accounts
from .core import BridgeError
from .models import MODELS, COMPAT_MODELS
from .account_routing import AccountRouter
from .tool_policy import NativeToolPolicy
from .backend_policy import BackendPolicy

class Hybrid:
    def __init__(self, root, native, gproxy_url, gproxy_key, *, enabled=False,
                 gproxy_enabled=True, concurrency=2, queue_size=24, queue_timeout=0,
                 timeout=0, allow_loopback=False, gproxy_models=None, output_token_overrides=None,
                 account_router=None, native_tool_choice_policy='strict', gproxy_only_models=None, policy_reader=None):
        self.root = Path(root); accounts.private_dir(self.root)
        self.native = native
        default = native.fixed_account or native.store.read().get('active')
        self.account_router = account_router or AccountRouter(native, allowed=[default], default=default, models=set(MODELS.values()))
        self.tool_policy = NativeToolPolicy(native_tool_choice_policy)
        initial = BackendPolicy.parse([] if gproxy_only_models is None else gproxy_only_models)
        self.backend_policy = policy_reader or (lambda: initial)
        self.url = urlsplit(gproxy_url)
        host = self.url.hostname
        private = False
        try: private = ipaddress.ip_address(host).is_private
        except ValueError: private = host == 'gproxy'
        if self.url.scheme != 'http' or not private or self.url.username or self.url.password or self.url.query or self.url.fragment:
            raise ValueError('Gproxy must be an explicitly configured private HTTP endpoint')
        if host in ('127.0.0.1', 'localhost') and not allow_loopback:
            raise ValueError('Loopback Gproxy is reserved for isolated tests')
        if not self.url.path.endswith('/chat/completions'): raise ValueError('Expected chat/completions path')
        if not 1 <= concurrency <= 2 or not 1 <= queue_size <= 128: raise ValueError('Invalid hybrid capacity')
        self.gproxy_key = gproxy_key
        overrides = {} if output_token_overrides is None else output_token_overrides
        if not isinstance(overrides, dict) or any(
                model not in set(MODELS.values()) or type(limit) is not int or limit < 1
                for model, limit in overrides.items()):
            raise ValueError('Output token overrides require exact configured models and positive integer limits')
        self.output_token_overrides = dict(overrides)
        self.gproxy_models = {m:m for m in COMPAT_MODELS} if gproxy_models is None else gproxy_models
        if set(self.gproxy_models) != COMPAT_MODELS or any(not isinstance(v,str) or not v for v in self.gproxy_models.values()):
            raise ValueError('Gproxy must explicitly map only compatibility-enabled models')
        self.enabled, self.gproxy_enabled = enabled, gproxy_enabled
        self.limit, self.max_queue = concurrency, queue_size
        self.queue_timeout, self.timeout = queue_timeout, timeout
        self.cv = threading.Condition(); self.waiting = []; self.running = {}; self.running_accounts = {}; self.running_plans = {}
        self.draining = False; self.force_cancel = threading.Event()
        self.audit_lock = threading.Lock(); self.guard_path = self.root/'gproxy-guard.json'
        with accounts.lock(self.root/'gproxy-guard.lock'):
            if not self.guard_path.exists(): accounts.atomic_json(self.guard_path, {})

    def gproxy_guard(self):
        return json.loads(self.guard_path.read_text())

    def ready_backends(self, plan=None, *, cli_allowed=True):
        available = []; errors = {}
        if cli_allowed:
            try:
                if not self.native.enabled or self.native.scheduler.draining:
                    raise BridgeError(503, 'cli_maintenance', 'CLI backend is not accepting new work')
                plan = plan or self.account_router.plan('')
                if plan.error: raise plan.error
                snap = plan.snapshot
                self.native.check_account(snap.alias, plan.model)
                reserved=list(self.running_accounts.values()).count(snap.alias)
                if reserved < self.native.leases.parallel_capacity(snap.alias) and not self.native.leases.is_busy(snap.alias): available.append('cli')
            except BridgeError as exc: errors['cli'] = exc
            except (OSError, accounts.AccountError):
                errors['cli'] = BridgeError(503, 'cli_unavailable', 'CLI account store is unavailable')
        if self.gproxy_enabled:
            state = self.gproxy_guard()
            if state.get('paused') or state.get('until', 0) > time.time():
                prior = state.get('error', {})
                errors['gproxy'] = BridgeError(state.get('status', 503), 'gproxy_guard',
                    prior.get('message', 'Gproxy backend is paused'), source='gproxy',
                    cached=True, dispatch_blocked=True, status_source='local_backend_guard',
                    **{k:prior[k] for k in ('upstream_reason','appeal_url','reason') if k in prior})
            else: available.append('gproxy')
        return available, errors

    def acquire(self, key, check, *, cli_allowed=True, native_plan=None, model=''):
        # Select once when admitted, not while waiting. An admitted request
        # retains its immutable account snapshot until it finishes.
        with self.cv:
            if not self.enabled: raise BridgeError(503, 'inference_disabled', 'Hybrid inference is disabled')
            if self.draining: raise BridgeError(503, 'draining', 'Hybrid service is draining')
            if len(self.waiting) >= self.max_queue: raise BridgeError(429, 'queue_full', 'Hybrid queue is full', retry_after=1)
            if model and model not in self.gproxy_models:
                if not cli_allowed:
                    raise BridgeError(400, 'native_tool_choice_unsupported', 'This CLI-only model cannot honor this tool selection; request not submitted')
                if not self.native.enabled:
                    raise BridgeError(503, 'cli_maintenance', 'The selected model requires CLI; request not submitted')
            self.waiting.append(key)
            try:
                while True:
                    check()
                    if self.draining: raise BridgeError(503, 'draining', 'Hybrid service is draining')
                    if self.waiting[0] == key and len(self.running) < self.limit:
                        reserved = {a:list(self.running_accounts.values()).count(a) for a in set(self.running_accounts.values())}
                        # A Gproxy-only decision cannot observe/reserve/mutate CLI accounts.
                        plan = (native_plan or self.account_router.plan(model, reserved=reserved)) if cli_allowed else None
                        available, errors = self.ready_backends(plan, cli_allowed=cli_allowed)
                        # An intended CLI-pool request waits for busy accounts and
                        # reports pool guards instead of silently using Gproxy12.
                        # Tool-incompatible requests retain their existing explicit
                        # Gproxy compatibility route before any CLI submission.
                        if cli_allowed and self.native.enabled and self.account_router.strategy == 'earliest_reset' and 'cli' not in available:
                            failure = plan.error or errors.get('cli')
                            # Dynamic catalogue mode may admit a new 4H turn to an
                            # available compatibility backend on local capacity or
                            # catalogue absence only. Never fail over a provider rejection.
                            local_capacity = failure is None or failure.code in ('accounts_busy','no_catalog_candidate')
                            use_compat = (model in getattr(self.account_router, 'catalog_models', ())
                                          and model in self.gproxy_models and 'gproxy' in available and local_capacity)
                            if not use_compat:
                                if failure is None or failure.code == 'accounts_busy':
                                    self.cv.wait(.05); continue
                                raise failure
                        # Prefer CLI. Its cross-process lease/refresh guard is checked
                        # locally, without starting a CLI or making a quota read.
                        if cli_allowed and 'cli' in available: selected = 'cli'
                        elif 'gproxy' in available and (not model or model in self.gproxy_models): selected = 'gproxy'
                        elif not cli_allowed:
                            raise errors.get('gproxy') or BridgeError(503, 'compatibility_unavailable', 'The selected model policy or tool selection requires Gproxy, which is unavailable; no request was submitted')
                        elif errors:
                            raise errors.get('cli') or errors['gproxy']
                        else:
                            self.cv.wait(.05); continue
                        if selected == 'cli': self.account_router.commit(plan)
                        self.waiting.remove(key); self.running[key] = selected
                        if selected == 'cli':
                            self.running_accounts[key] = plan.alias
                            self.running_plans[key] = plan
                        self.cv.notify_all(); return selected
                    self.cv.wait(.05)
            except BaseException:
                if key in self.waiting: self.waiting.remove(key)
                self.cv.notify_all(); raise

    def selected_plan(self, key):
        with self.cv: return self.running_plans.get(key)

    def release(self, key):
        with self.cv:
            self.running.pop(key, None); self.running_accounts.pop(key, None); self.running_plans.pop(key, None); self.cv.notify_all()

    def drain(self):
        with self.cv: self.draining = True; self.cv.notify_all()

    def record_error(self, status, error):
        if status not in (401, 403, 429): return
        with accounts.lock(self.root/'gproxy-guard.lock'):
            accounts.atomic_json(self.guard_path, {
                'status': status, 'paused': status in (401, 403),
                'until': time.time()+error.details.get('retry_after',60) if status==429 else 0,
                'error':error.public()['error'], 'at':time.time()})

    def audit(self, data):
        with self.audit_lock:
            fd=os.open(self.root/'routing.jsonl',os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as stream: stream.write(json.dumps(data)+'\n')

    def status(self):
        # Invalid hot config must be observable without breaking drain/status.
        try:
            policy = self.backend_policy()
        except BridgeError:
            policy = None
        # Status reads do not even take a temporary account execution lease.
        with self.cv:
            return {'version':__version__, 'enabled':self.enabled, 'draining':self.draining,
                    'active':len(self.running),'queued':len(self.waiting),'global_limit':self.limit,
                    'active_by_backend':{b:list(self.running.values()).count(b) for b in ('cli','gproxy')},
                    'preference':(['cli'] if self.native.enabled else []) + (['gproxy'] if self.gproxy_enabled else []), 'retry_after_dispatch':False,
                    'output_token_overrides':dict(self.output_token_overrides),
                    'gproxy_enabled':self.gproxy_enabled, 'gproxy_guard':self.gproxy_guard(),
                    'route_policy_ready':policy is not None, 'route_policy_revision':policy.revision if policy else None,
                    'gproxy_only_models':sorted(policy.pins) if policy else None,
                    'model_preferences':{model: (['gproxy'] if model in policy.pins else (['cli'] if self.native.enabled else []) + (['gproxy'] if self.gproxy_enabled and model in self.gproxy_models else [])) for model in sorted(set(MODELS.values()))} if policy else {},
                    'timeouts':{'request':self.timeout,'queue':self.queue_timeout,'native_read':None,'gproxy_socket':self.timeout or None},
                    'native_tool_choice_policy':self.tool_policy.mode,
                    'account_routing':self.account_router.status(),
                    'active_by_account':{alias:list(self.running_accounts.values()).count(alias) for alias in set(self.running_accounts.values())},
                    'native':dict(self.native.status(), selected_account=self.account_router.default if self.account_router.strategy == 'fixed' else None)}


