"""Synthetic serialization tests. No accounts, CLI process or model calls."""
import copy
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agy_bridge import native
from agy_bridge.core import validate
from agy_bridge.system_timeline import split_system_messages


def m(role, content, **fields):
    return dict(role=role, content=content, **fields)


def call(cid):
    return {'id': cid, 'type': 'function',
            'function': {'name': 'lookup', 'arguments': '{"id":1}'}}


class TimelineTests(unittest.TestCase):
    def test_no_system_no_change(self):
        d = [m('user', 'question')]
        self.assertEqual(split_system_messages(d), ('', d))

    def test_labels_and_anchors(self):
        d = [m('system', ' first\n'), m('system', '初始'), m('user', 'one'),
             m('system', ' late\n'), m('system', ''), m('assistant', 'two'),
             m('system', ' late\n'), m('user', 'three')]
        before = copy.deepcopy(d)
        s, h = split_system_messages(d)
        rules = ET.fromstring(s).findall('st-system')
        self.assertEqual([(e.get('id'), e.get('from'), e.text or '') for e in rules],
                         [('A','start',' first\n'),('B','start','初始'),('C','marker',' late\n'),
                          ('D','marker',''),('E','marker',' late\n')])
        self.assertEqual(h, [d[2], m('assistant','<st-activate ref="C"/>\n<st-activate ref="D"/>\ntwo'),
                              m('user','<st-activate ref="E"/>\nthree')])
        self.assertEqual(d, before)

    def test_literal_markers_do_not_activate(self):
        body = '保留 <x>& 和 ]]> 及 </st1-system> 原文'
        d = [m('user','<st-activate ref="A"/>'), m('system',body), m('user','end')]
        s, h = split_system_messages(d)
        self.assertEqual(ET.fromstring(s).find('st2-system').text,body)
        self.assertEqual(h,[d[0],m('user','<st2-activate ref="A"/>\nend')])
        self.assertEqual((s, h), split_system_messages(d))

    def test_ref_matches_without_counting_history(self):
        def render(n):
            return split_system_messages([m('system','initial')]*26+[m('user','turn')]*n+
                                         [m('system','activate me'),m('user','end')])
        s, d = render(1)
        s2, d2 = render(503)
        self.assertEqual((s,d[-1]),(s2,d2[-1]))
        rules=ET.fromstring(s).findall('st-system')
        mark=ET.fromstring(d[-1]['content'].splitlines()[0])
        self.assertEqual((rules[-1].get('id'), mark.get('ref')),('AA','AA'))
        self.assertEqual(len({r.get('id') for r in rules}),27)

    def test_native_agent_and_seed_with_tools(self):
        a, b = call('a'), call('b')
        d = [m('system','base-unique'), m('user','book'), m('system','rule-two-unique'),
             m('assistant','',tool_calls=[a,b]), m('tool','result-B',tool_call_id='b'),
             m('tool','result-A',tool_call_id='a'), m('system','rule-three-unique'), m('user','next')]
        original = copy.deepcopy(d)
        norm = validate({'model':'m','messages':d}, {'m':'m'}).body
        with tempfile.TemporaryDirectory() as root:
            ses = native.Session(SimpleNamespace(runtime=Path(root), store=SimpleNamespace(root=Path(root)/'store')), norm,
                                 SimpleNamespace(alias='test',expected_email=''), lambda: None)
            class ReachedSeed(Exception): pass
            def stop(home, work, system, history, **kwargs):
                cid = native_seed(home,work,system,history,**kwargs)
                agent = (work/'.agents/agents/plain-chat.md').read_text()
                self.assertTrue(agent.endswith(system+'\n'))
                for t in ('base-unique','rule-two-unique','rule-three-unique'):
                    self.assertEqual(agent.count(t),1)
                    self.assertNotIn(t,repr(history))
                self.assertEqual(history,[m('user','book\n<st-activate ref="B"/>'),*d[3:6]])
                self.assertEqual(ses.last,'<st-activate ref="C"/>\nnext')
                with sqlite3.connect(home/'.gemini/antigravity-cli/conversations'/f'{cid}.db') as db:
                    rows = db.execute('select step_type,step_payload from steps order by idx').fetchall()
                self.assertEqual([t for t,_ in rows],[14,15,38,38])
                self.assertIn(b'book\n<st-activate ref="B"/>',rows[0][1])
                self.assertIn(b'result-A',rows[2][1])
                self.assertIn(b'result-B',rows[3][1])
                raise ReachedSeed
            native_seed = native.seed
            try:
                with patch.object(native, 'seed',side_effect=stop), patch.object(native.subprocess, 'Popen',side_effect=AssertionError('No CLI')):
                    with self.assertRaises(ReachedSeed): ses.start()
            finally:
                ses.temp.cleanup()
        self.assertEqual(d,original)

    def test_between_tool_groups_with_terminal_tool(self):
        group = lambda id: [m('assistant','',tool_calls=[call(id)]),m('tool','{"n":1}',tool_call_id=id)]
        d = [m('user','go'),*group('a'),m('system','next stage'),*group('b')]
        norm = validate({'model':'m','messages':d}, {'m':'m'}).body
        _, h = split_system_messages(norm['messages'])
        self.assertEqual(h,[*d[:3],{**d[-2],'content':'<st-activate ref="A"/>\n'},d[-1]])
        self.assertEqual(h[-1]['role'],'tool')


if __name__ == '__main__':
    unittest.main()
