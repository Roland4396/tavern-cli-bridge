import tempfile
import unittest
from types import SimpleNamespace
from agy_bridge.models import MODELS, EXPLICIT_NATIVE, COMPAT_MODELS, NONSTREAM_MODELS, guard_model
from agy_bridge.account_routing import AccountRouter
from agy_bridge.quota import QuotaStore


class SubscriptionTiers(unittest.TestCase):
    def test_exact_names_and_stable_aliases(self):
        self.assertEqual(len(EXPLICIT_NATIVE), 11)
        for model in EXPLICIT_NATIVE:
            self.assertEqual(MODELS[model], model)
        self.assertFalse(EXPLICIT_NATIVE & (COMPAT_MODELS | NONSTREAM_MODELS))
        self.assertEqual(MODELS['gemini-4-hydrogen'], 'gemini-4-hydrogen-xhigh')
        self.assertEqual(MODELS['gemini-4-argon'], 'gemini-4-argon-medium')
        self.assertNotIn('claude-opus-5-5', MODELS)

    def test_no_implicit_account_pool(self):
        model='gemini-4-neon-low'
        stub=SimpleNamespace(catalog_models=frozenset(), model_accounts={}, strategy='earliest_reset',allowed=('test-a','test-b'))
        self.assertEqual(AccountRouter.pool_for(stub, model), ())
        stub.model_accounts[model]=('test-a',)
        self.assertEqual(AccountRouter.pool_for(stub, model), ('test-a',))
        self.assertEqual(guard_model(model), 'gemini-4-neon-xhigh')
        self.assertEqual(guard_model('gemini-4-argon-low'), 'gemini-4-argon-high')

    def test_undated_third_party_quota_and_separate_families(self):
        with tempfile.TemporaryDirectory() as d:
            store=QuotaStore(d)
            def bucket(key,n,**extra):
                store.observe_summary('test-a', {'response':{'groups':[{'buckets':[dict(bucketId=key, remainingFraction=n, **extra)]}]}})
            for key in ('3p-weekly','3p-5h'): bucket(key,1)
            model='claude-sonnet-5-5-low'
            self.assertFalse(store.standing('test-a',model).get('blocked_without_reset'))
            bucket('3p-weekly',0)
            self.assertTrue(store.standing('test-a',model)['blocked_without_reset'])
            bucket('3p-5h',1)
            self.assertTrue(store.standing('test-a',model)['blocked_without_reset'])
            self.assertFalse(store.standing('test-a','gemini-4-neon-high').get('blocked_without_reset'))
            bucket('3p-weekly',1)
            self.assertFalse(store.standing('test-a',model).get('blocked_without_reset'))
            bucket('gemini-5h',0,resetTime='2099-01-01T00:00:00Z')
            for name in ('gemini-4-neon-medium','gemini-4-argon-low'):
                self.assertIsNotNone(store.standing('test-a',name)['blocked_until'])
            self.assertIsNone(store.standing('test-a',model)['blocked_until'])
            self.assertIsNone(store.standing('test-b','gemini-4-argon-low')['blocked_until'])

    def test_exact_zero_survives_shared_full_summary(self):
        with tempfile.TemporaryDirectory() as d:
            store=QuotaStore(d); model='claude-opus-5-5-low'
            store.observe_catalog('test-a',{'clientModelConfigs':[{'modelId':model,'quotaInfo':{
                'resetTime':'2099-01-01T00:00:00Z','remainingFraction':0}}]})
            store.observe_summary('test-a',{'response':{'groups':[{'buckets':[
                {'bucketId':k,'remainingFraction':1} for k in ('3p-weekly','3p-5h')]}]}})
            self.assertIsNotNone(store.standing('test-a',model)['blocked_until'])
