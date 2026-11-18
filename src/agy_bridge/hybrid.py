"""CLI-first, admission-only hybrid routing; no replay after a backend is selected.
One CPU control process, two total jobs, fresh native session for every CLI job.
Only explicit client POSTs can start a worker or contact either model backend.
"""
import argparse
import copy
import fcntl
import http.client
import ipaddress
import json
import os
import signal
import socket
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from . import account_store as accounts
from . import __version__
from .core import BridgeError, validate, upstream_error, native_tool_choice_supported
from .server import Application, UnixServer
from .http_transport import TransportHandler
from .execution import serve_native
from .compatibility import serve_gproxy
from .routing import Hybrid
from .backend_policy import FilePolicy, FIELD
from .account_routing import AccountRouter
from .native import NativeBackend
from .quota import QuotaStore
from .nonstream_runtime import NonstreamRuntime
from .keeper_control import KeeperControl, PREFIX as KEEPER_PREFIX

from .models import MODELS, NONSTREAM_MODELS

def private_token(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('Token file must be private and not a symlink')
    value = path.read_text().strip()
    if len(value) < 24: raise ValueError('Token is too short')
    return value


class HybridHandler(TransportHandler):
    def application(self): return self.server.app.native

    def send_response(self, code, message=None):
        self.sent_status=code
        return super().send_response(code,message)

    def end_headers(self):
        if hasattr(self,'selected'):
            self.send_header('X-AGY-Backend',self.selected)
            self.send_header('X-AGY-Hybrid-ID',self.route_id)
            if self.selected == 'cli' and hasattr(self, 'tool_decision'):
                self.send_header('X-AGY-Account-Strategy', self.server.app.account_router.strategy)
                for key, value in self.tool_decision.headers().items():
                    self.send_header(key, value)
        return super().end_headers()

    def do_GET(self):
        if self.path.startswith(KEEPER_PREFIX): return self.server.app.keeper_control.handle(self)
        if not self.authorized(): return self.reply(401, BridgeError(401,'unauthorized','Unauthorized').public())
        if self.path=='/healthz':
            app=self.server.app;s=app.status();proxy=getattr(app,'nonstream_proxy',None)
            transport=proxy.status() if proxy else {'models':[],'ready':True,'mode':'native-default','body_recording':False}
            return self.reply(200, {'ok':not s['draining'] and s['route_policy_ready'] and transport['ready'],**s,'native_upstream_transport':transport})
        if self.path=='/v1/models':
            return self.reply(200, {'object':'list','data':[{'id':m,'object':'model','created':0,'owned_by':'agy-hybrid'} for m in MODELS]})
        return self.reply(404, BridgeError(404,'not_found','Not found').public())

    def do_POST(self):
        if self.path.startswith(KEEPER_PREFIX): return self.server.app.keeper_control.handle(self)
        if not self.authorized(): return self.reply(401,BridgeError(401,'unauthorized','Unauthorized').public())
        if self.path!='/v1/chat/completions': return self.reply(404,BridgeError(404,'not_found','Not found').public())
        app=self.server.app; self.route_id='hybrid-'+uuid.uuid4().hex
        started=time.monotonic(); acquired=False; model=None; native_plan=None; policy=None
        def check():
            if self.gone(): raise BridgeError(499,'client_disconnected','Client disconnected')
            if app.force_cancel.is_set(): raise BridgeError(503,'drain_timeout','Drain deadline reached')
            if app.timeout > 0 and time.monotonic()-started > app.timeout: raise BridgeError(504,'timeout','Hybrid request deadline exceeded')
            if app.queue_timeout > 0 and not acquired and time.monotonic()-started > app.queue_timeout: raise BridgeError(504,'queue_timeout','Hybrid queue deadline exceeded')
        try:
            if self.headers.get('X-AGY-Account'): raise BridgeError(403,'account_override_denied','Hybrid account selection is server-owned')
            policy=app.backend_policy()  # immutable for this entire request, including its queue wait
            body=self.read_json()
            if not isinstance(body,dict) or not isinstance(body.get('model'),str) or body['model'] not in MODELS: raise BridgeError(400,'model_unavailable','Requested model is not in the bridge catalog')
            body['model']=MODELS[body['model']];model=body['model']
            if model in app.output_token_overrides:
                body['max_tokens'] = app.output_token_overrides[model]
                body.pop('max_completion_tokens', None)
            prepared=validate(body,set(MODELS.values()))
            self.tool_decision=app.tool_policy.apply(prepared)
            check();self.selected=app.acquire(self.route_id,check,cli_allowed=model not in policy.pins and native_tool_choice_supported(self.tool_decision.request.body),model=model);acquired=True
            native_plan=app.selected_plan(self.route_id);check()
            if self.selected=='cli':
                return serve_native(self, app.native, self.tool_decision.request,
                                    snapshot=native_plan.snapshot, external_check=check,
                                    audit_extra=self.tool_decision.audit())
            return serve_gproxy(self, app, body, check)
        except BridgeError as exc:
            self.completion_result=exc.code
            if exc.status!=499:
                try:self.reply(exc.status,exc.public(),{'X-AGY-Dispatch':'not-submitted'})
                except OSError:pass
        except (OSError,ValueError):
            self.completion_result='request_read_error'
            if not hasattr(self,'sent_status'):
                try:self.reply(400,BridgeError(400,'request_read_error','Request body could not be read').public())
                except OSError:pass
        finally:
            try:
                selected=getattr(self,'selected',None)
                app.audit({'id':self.route_id,'backend':selected,'model':model,
                           'account':native_plan.alias if selected=='cli' and native_plan else None,
                           'status':getattr(self,'sent_status',None),'result':getattr(self,'completion_result','unknown'),'seconds':round(time.monotonic()-started,3),
                           'backend_dispatches':int(acquired),'automatic_retry':False,
                           'account_strategy':app.account_router.strategy,
                           'account_selection_candidates':list(native_plan.candidates) if native_plan else [],
                           'account_selection_reset_at':native_plan.reset_at if native_plan else None,
                           'backend_policy':policy.mode(model) if model and policy else None,
                           'route_policy_revision':policy.revision if policy else None,
                           **(self.tool_decision.audit() if selected=='cli' else {})})
            finally:
                if acquired:app.release(self.route_id)
                self.close_connection=True



def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True);args=parser.parse_args()
    if args.config.stat().st_mode & 0o077:parser.error('Config must be private')
    cfg=json.loads(args.config.read_text());root=Path(cfg['root']);accounts.private_dir(root)
    lock=os.open(root/'server.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:parser.error('This runtime already has a server')
    store=accounts.Accounts(cfg['accounts_root'])
    # Hash verification is CPU/disk-only. No CLI is launched here.
    from .native import MODEL_ENUMS
    if cfg.get('enabled') and cfg.get('native_enabled', True) and not MODEL_ENUMS:
        parser.error('Native generation requires a version-matched local model enum mapping')
    backend=NativeBackend(cfg['request_root'],store)
    backend.quota_store=QuotaStore(root/'native')
    transport_models=cfg.get('native_nonstream_models',[])
    if not isinstance(transport_models,list) or not all(isinstance(m,str) for m in transport_models) or not set(transport_models)<=NONSTREAM_MODELS:
        parser.error('native_nonstream_models must contain exact configured model IDs')
    backend.nonstream_models=frozenset(transport_models)
    backend.nonstream_state=root/'nonstream-proxy'
    nonstream_proxy=NonstreamRuntime(backend.nonstream_state,transport_models).start() if transport_models else None
    native=Application(root/'native',store,backend,enabled=bool(cfg.get('enabled')) and bool(cfg.get('native_enabled', True)),
                       models=set(MODELS.values()),fixed_account=None,
                       timeout=cfg.get('timeout',0),queue_timeout=cfg.get('queue_timeout',0))
    app=Hybrid(root/'hybrid',native,cfg['gproxy_url'],private_token(cfg['gproxy_key_file']),
               enabled=bool(cfg.get('enabled')),gproxy_enabled=cfg.get('gproxy_enabled',True),
               timeout=cfg.get('timeout',0),queue_timeout=cfg.get('queue_timeout',0),
               gproxy_models=cfg.get('gproxy_models'),output_token_overrides=cfg.get('output_token_overrides'),
               account_router=AccountRouter.from_config(native,cfg,set(MODELS.values())),
               native_tool_choice_policy=cfg.get('native_tool_choice_policy','strict'),
               gproxy_only_models=cfg.get(FIELD),policy_reader=FilePolicy(args.config,field_required=FIELD in cfg))
    app.nonstream_proxy=nonstream_proxy
    keeper_key = private_token(cfg['quota_keeper_key_file']) if cfg.get('quota_keeper_key_file') else None
    if keeper_key and keeper_key == private_token(cfg['api_key_file']):
        parser.error('Quota keeper control requires a separate token')
    app.keeper_control = KeeperControl(app, args.config, cfg.get('native_accounts', []), keeper_key)
    server=UnixServer(cfg['socket'],app,private_token(cfg['api_key_file']));server.RequestHandlerClass=HybridHandler
    stopping=threading.Event()
    def shutdown(signum,frame):
        if stopping.is_set():return
        stopping.set();app.drain()
        def drain():
            deadline=time.monotonic()+cfg.get('drain_timeout',0)
            while app.status()['active'] and (cfg.get('drain_timeout',0) == 0 or time.monotonic()<deadline):time.sleep(.1)
            if app.status()['active']:app.force_cancel.set();native.force_cancel.set()
            native.scheduler.drain()
            end=time.monotonic()+15
            while app.status()['active'] and time.monotonic()<end:time.sleep(.1)
            server.shutdown()
        threading.Thread(target=drain,daemon=True).start()
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
    print(json.dumps({'event':'ready','version':__version__,'enabled':app.enabled,'transport':'private_unix','models':list(MODELS)}),flush=True)
    try:server.serve_forever(poll_interval=.1)
    finally:
        server.server_close()
        if nonstream_proxy:nonstream_proxy.close()
        os.close(lock)

if __name__=='__main__':main()
