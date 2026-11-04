import asyncio
import copy
from pathlib import Path
import tempfile
import unittest

from evaluation_pipeline import OfflineEvaluationPipeline
from host_adapter import Binding
from relation_core import RelationStore, Subject


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.binding = Binding('qq','999')
        self.store = RelationStore(Path(self.tmp.name)/self.binding.database_name())
        self.msg = {'platform':'qq','account_id':'999','scope':'','session_id':'s',
                    'message_id':'m','is_notify':False,'is_command':False,
                    'processed_plain_text':'谢谢你',
                    'message_info':{'user_info':{'user_id':'111'},'group_info':None}}
        self.calls = []

    async def model(self, messages):
        self.calls.append(copy.deepcopy(messages))
        return '{"kind":"meaningful_support","delta":1}'

    async def lookup(self, mid, **kw):
        self.assertEqual((mid,kw['chat_id']),('m','s'))
        return copy.deepcopy(self.msg)

    async def test_automatic_low_only(self):
        p = OfflineEvaluationPipeline(self.store,self.binding,self.model,self.lookup)
        result = await p.evaluate(self.msg)
        self.assertTrue(result.accepted)
        self.assertEqual(result.state.manual_grant,0)
        self.assertLessEqual(result.state.auto_level,2)
        self.assertNotIn('999',str(self.calls))
        self.assertNotIn('111',str(self.calls))

    async def test_changed_author_after_wait_no_write(self):
        async def changed(mid, **kw):
            m = copy.deepcopy(self.msg); m['message_info']['user_info']['user_id']='222'; return m
        p = OfflineEvaluationPipeline(self.store,self.binding,self.model,changed)
        with self.assertRaises(ValueError): await p.evaluate(self.msg)
        self.assertEqual(self.store.projection(Subject('qq','111'),'self_private',Subject('qq','111'))['score'],0)

    async def test_simultaneous_duplicate_one_evaluation(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(messages):
            started.set(); await release.wait(); return await self.model(messages)
        p = OfflineEvaluationPipeline(self.store,self.binding,slow,self.lookup)
        task = asyncio.create_task(p.evaluate(self.msg)); await started.wait()
        self.assertIsNone(await p.evaluate(self.msg)); release.set(); await task
        self.assertEqual(len(self.calls),1)

    async def test_failure_releases_inflight(self):
        async def fail(messages): raise RuntimeError('offline failure')
        p = OfflineEvaluationPipeline(self.store,self.binding,fail,self.lookup)
        with self.assertRaises(RuntimeError): await p.evaluate(self.msg)
        self.assertFalse(p._inflight)

    async def test_invalid_target_no_model_call(self):
        p = OfflineEvaluationPipeline(self.store,self.binding,self.model,self.lookup)
        m = copy.deepcopy(self.msg); m['account_id']='other'
        with self.assertRaises(ValueError): await p.evaluate(m)
        self.assertFalse(self.calls)


if __name__ == '__main__': unittest.main()
