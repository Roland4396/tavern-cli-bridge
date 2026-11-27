import tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from agy_bridge.account_routing import AccountRouter
from agy_bridge.models import MODELS,CARBON
from agy_bridge.core import BridgeError

H='gemini-4-hydrogen-xhigh'
class Selection(unittest.TestCase):
 def test_enabled_catalogue_quota_guard_busy_and_fixed_carbon(self):
  with tempfile.TemporaryDirectory() as tmp:
   guards=set(); busy=set()
   def guard(alias,m):
    if alias in guards:raise BridgeError(403,'account_permission_denied','blocked')
   native=SimpleNamespace(root=Path(tmp), resolve=lambda a: SimpleNamespace(alias=a),
       check_account=guard, leases=SimpleNamespace(is_busy=lambda a:a in busy,parallel_capacity=lambda a:1))
   cfg=dict(native_accounts=['a','b','c'],native_default_account='a',native_model_accounts={m:['b'] for m in CARBON},
            native_account_strategy='earliest_reset',native_catalog_models=[H])
   r=AccountRouter.from_config(native,cfg,set(MODELS.values()))
   def see(a,names):r.quotas.observe_catalog(a,{'clientModelConfigs':names})
   def enabled():return list(r.pool_for(H))
   self.assertEqual(enabled(),[])
   see('a',[dict(modelId=H)]);see('b',[dict(modelId=H,disabled=True)]);see('c',[dict(modelId='other')])
   self.assertEqual(enabled(),['a']);see('b',[dict(modelId=H)]);self.assertEqual(enabled(),['a','b'])
   for m in CARBON:self.assertEqual(r.plan(m).alias,'b')
   p=r.plan(H);r.commit(p);self.assertNotEqual(r.plan(H).alias,p.alias)
   busy.add('a');self.assertEqual(r.plan(H).alias,'b')
   busy.clear();guards.add('b');self.assertEqual(r.plan(H).alias,'a')
   see('a',[dict(modelId='other')]);self.assertEqual(r.plan(H).error.status,403)
   guards.clear();r.quotas.merge('b',{H:{'gemini-weekly':dict(reset_at=9999999999,observed_at=1,remaining_fraction=0)}})
   self.assertEqual(r.plan(H).error.status,429)
   self.assertEqual(r.pool_for('gemini-4-argon-high'),('a','b','c'))
 def test_dynamic_compat_admission_never_retries_guards(self):
  from agy_bridge.routing import Hybrid
  from agy_bridge.account_routing import AccountPlan
  from threading import Condition
  for code, expected in (('accounts_busy','gproxy'),('no_catalog_candidate','gproxy'),('quota_exhausted',None),('account_permission_denied',None),('cli_unavailable',None)):
   failure=BridgeError(503,code,'test')
   plan=AccountPlan('',error=failure)
   app=SimpleNamespace(cv=Condition(),enabled=True,draining=False,waiting=[],limit=2,max_queue=8,running={},running_accounts={},
       native=SimpleNamespace(enabled=True),gproxy_models={H:'mock'},
       account_router=SimpleNamespace(strategy='earliest_reset',catalog_models={H},plan=lambda *a,**kw:plan),
       ready_backends=lambda *a,**kw:(['gproxy'],{'cli':failure}))
   if expected:
    self.assertEqual(Hybrid.acquire(app,'test',lambda:None,model=H),expected)
   else:
    with self.assertRaises(BridgeError) as ctx:Hybrid.acquire(app,'test',lambda:None,model=H)
    self.assertIs(ctx.exception,failure);self.assertEqual(app.running,{})
if __name__=='__main__':unittest.main()
