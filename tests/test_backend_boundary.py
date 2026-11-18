"""Speak real HTTP to the handler and a loopback-only synthetic provider."""
import concurrent.futures
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from agy_bridge import hybrid
from agy_bridge.backend_policy import FilePolicy
from agy_bridge.routing import Hybrid
from agy_bridge.core import BridgeError

MODEL='gemini-4-hydrogen-xhigh'
ALIAS='gemini-4-hydrogen'
KEY='synthetic-test-only'

class DummyProvider(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.accepted.append(body)
        text=body['messages'][0]['content']
        if text in ('hold','drop'):
            self.server.received.set()
            if not self.server.release.wait(5): return
        if text=='drop':
            self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
        raw=json.dumps({'model':body['model'],'choices':[{'message':{'role':'assistant','content':'fixture'}}]}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)


def start(handler):
    s=ThreadingHTTPServer(('127.0.0.1',0),handler)
    t=threading.Thread(target=s.serve_forever,kwargs={'poll_interval':.02},daemon=True);t.start()
    return s,t

class BackendBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.cfg=self.root/'bridge.json';self.publish([MODEL])
        self.provider,pt=start(DummyProvider);self.provider.accepted=[]
        self.provider.received=threading.Event();self.provider.release=threading.Event()
        self.addCleanup(self.stop,self.provider,pt)
        self.router=Mock(strategy='earliest_reset')
        self.router.plan.return_value=NS(alias='fixture',model=MODEL,snapshot=NS(alias='fixture'),error=None,candidates=('fixture',),reset_at=None)
        n=Mock(fixed_account='fixture',enabled=True,scheduler=NS(draining=False))
        n.leases.parallel_capacity.return_value=1;n.leases.is_busy.return_value=False
        n.status.return_value={};self.router.status.return_value={}
        self.app=Hybrid(self.root/'state',n,f'http://127.0.0.1:{self.provider.server_port}/chat/completions',KEY,
                        enabled=True,allow_loopback=True,concurrency=1,account_router=self.router,
                        policy_reader=FilePolicy(self.cfg),output_token_overrides={MODEL:90000})
        self.bridge,bt=start(hybrid.HybridHandler); self.bridge.token=KEY;self.bridge.app=self.app
        self.addCleanup(self.stop,self.bridge,bt)
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=3);self.addCleanup(self.pool.shutdown)
        self.addCleanup(self.provider.release.set)
        def native(handler,*args,**kwargs):
            handler.completion_result='ok';return handler.reply(200,{'choices':[{'message':{'content':'fixture-cli'}}]})
        self.cli=Mock(side_effect=native); stub=patch.object(hybrid,'serve_native',self.cli);stub.start();self.addCleanup(stub.stop)
    def tearDown(self):
        self.provider.release.set()
    def stop(self,server,thread):
        server.shutdown();server.server_close();thread.join(2)
    def publish(self,pins):
        p=self.cfg.with_suffix('.tmp');p.write_text(json.dumps({'gproxy_only_models':pins}));p.chmod(0o600);p.replace(self.cfg)
    def call(self,content):
        c=http.client.HTTPConnection('127.0.0.1',self.bridge.server_port,timeout=6)
        try:
            body={'model':ALIAS,'messages':[{'role':'user','content':content}],'stream':False,'max_completion_tokens':42}
            c.request('POST','/v1/chat/completions',json.dumps(body),{'Authorization':'Bearer '+KEY})
            r=c.getresponse();return r.status,r.getheader('X-AGY-Backend'),r.getheader('X-AGY-Dispatch'),json.loads(r.read())
        finally:c.close()
    def queued(self):
        until=time.monotonic()+3
        while time.monotonic()<until:
            with self.app.cv:
                if self.app.waiting:return
            time.sleep(.005)
        self.fail('No request reached the queue')
    def test_excluded_cli_is_never_consulted(self):
        self.router.plan.side_effect=AssertionError('excluded account store touched')
        self.assertEqual(self.call('one')[:2],(200,'gproxy'))
        self.assertEqual(len(self.provider.accepted),1);body=self.provider.accepted[0]
        self.assertEqual((body['model'],body['max_tokens'],body['stream']),(MODEL,90000,False))
        self.assertNotIn('max_completion_tokens',body)
        self.router.plan.assert_not_called();self.app.native.check_account.assert_not_called()
    def test_queued_policy_frozen_account_not_preallocated(self):
        first=self.pool.submit(self.call,'hold');self.assertTrue(self.provider.received.wait(3))
        waiting=self.pool.submit(self.call,'old-policy');self.queued()
        self.publish([]);self.router.plan.assert_not_called()
        self.provider.release.set()
        self.assertEqual(first.result()[:2],(200,'gproxy'));self.assertEqual(waiting.result()[:2],(200,'gproxy'))
        self.assertEqual(self.call('new-policy')[:2],(200,'cli'))
        self.assertEqual(self.router.plan.call_count,1);self.assertEqual(self.cli.call_count,1)
        self.assertEqual(len(self.provider.accepted),2)
    def test_changed_guard_still_applies_at_admission(self):
        first=self.pool.submit(self.call,'hold');self.assertTrue(self.provider.received.wait(3))
        waiting=self.pool.submit(self.call,'queued');self.queued()
        self.app.record_error(403,BridgeError(403,'provider_permission','synthetic refusal'))
        self.provider.release.set();self.assertEqual(first.result()[0],200)
        code,_,dispatch,body=waiting.result();self.assertEqual((code,dispatch),(403,'not-submitted'))
        self.assertEqual(body['error']['code'],'gproxy_guard');self.assertEqual(len(self.provider.accepted),1)
        self.router.plan.assert_not_called()
    def test_post_accept_disconnect_never_replays(self):
        pending=self.pool.submit(self.call,'drop');self.assertTrue(self.provider.received.wait(3))
        self.publish([]);self.provider.release.set()
        code,backend,mark,_=pending.result()
        self.assertEqual((code,backend,mark),(502,'gproxy','submitted-or-uncertain'))
        self.assertEqual(len(self.provider.accepted),1);self.cli.assert_not_called()
    def test_invalid_update_does_not_enable_another_backend(self):
        for bad in (['unlisted'],None,42):
            self.publish(bad)
            code,_,dispatch,body=self.call('denied')
            self.assertEqual((code,dispatch,body['error']['code']),(503,'not-submitted','route_policy_unavailable'))
            self.assertFalse(self.app.status()['route_policy_ready'])
        self.cfg.write_text('{}')
        self.assertEqual(self.call('missing')[0],503)
        self.assertFalse(self.provider.accepted);self.router.plan.assert_not_called()
        self.publish([MODEL]);self.assertEqual(self.call('recovered')[:2],(200,'gproxy'))

if __name__=='__main__':unittest.main()
