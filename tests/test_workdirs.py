import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from agy_bridge.native import Session
from agy_bridge.workdirs import account_parent
from agy_bridge.account_store import AccountError


class Workdirs(unittest.TestCase):
    def test_persistence_concurrency_and_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); runtime = root/'runtime'; store = root/'store'
            backend = SimpleNamespace(mock_upstream=True,runtime=runtime,store=SimpleNamespace(root=store))
            def session(a):return Session(backend,{},SimpleNamespace(alias=a),lambda:None)
            with ThreadPoolExecutor(8) as pool:
                sessions=list(pool.map(session,['test_a','test_b']*8))
            dirs={a:{s.base.parent for s in sessions if s.snapshot.alias==a} for a in ('test_a','test_b')}
            self.assertTrue(all(len(v)==1 for v in dirs.values()))
            self.assertFalse(dirs['test_a'] & dirs['test_b'])
            self.assertEqual(len({s.base for s in sessions}),16)
            for s in sessions:
                self.assertEqual(s.base.parent.stat().st_mode&0o777,0o700)
                self.assertRegex(s.base.parent.name,r'^account-[0-9a-f]{32}$')
                self.assertEqual(s.home.parent,s.work.parent);self.assertEqual(s.work.parent,s.base)
                s.close();self.assertFalse(s.base.exists())
            p=next(iter(dirs['test_a']));p.rmdir()
            self.assertEqual(account_parent(runtime,store,'test_a'),p)
    def test_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); store=root/'store'; path=account_parent(root,store,'a')
            with self.assertRaises(AccountError): account_parent(root,store,'../bad')
            path.rmdir();path.symlink_to(root)
            with self.assertRaises(AccountError):account_parent(root,store,'a')
            idx=store/'workspace-index'/'names.json'
            idx.write_text(json.dumps({'a':'../bad'}))
            with self.assertRaises(AccountError):account_parent(root,store,'a')
