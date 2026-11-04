import copy
from pathlib import Path
import tempfile
import unittest

from evaluation import EvaluationSubject
from evaluation_binding import Receipt, apply_bound_result
from host_adapter import Binding
from relation_core import RelationStore, Subject, VersionConflict


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.binding = Binding('qq', '999', '')
        self.admin = Subject('qq', '1000000001')
        self.store = RelationStore(Path(self.tmp.name)/self.binding.database_name(), admin_subjects=[self.admin])
        self.receipt = Receipt(EvaluationSubject('qq','999','111','s1','m1'),0)
        self.message = {'platform':'qq','account_id':'999','scope':'','session_id':'s1',
                        'message_id':'m1','is_notify':False,'is_command':False,
                        'message_info':{'user_info':{'user_id':'111'},'group_info':None}}
        self.raw = '{"kind":"meaningful_support","delta":1}'

    def apply(self, message=None, receipt=None, raw=None):
        return apply_bound_result(self.store,receipt or self.receipt,
                                  message if message is not None else self.message,
                                  self.binding,raw or self.raw)

    def test_valid_and_duplicate(self):
        self.assertTrue(self.apply().accepted)
        newer = Receipt(self.receipt.subject,1)
        self.assertEqual(self.apply(receipt=newer).reason,'duplicate')

    def test_cross_user_and_route_never_write(self):
        for key, value in [('platform','other'),('account_id','888'),('scope','other'),
                           ('session_id','s2'),('message_id','m2'),('is_notify',True)]:
            m = copy.deepcopy(self.message); m[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.apply(message=m)
        m = copy.deepcopy(self.message); m['message_info']['user_info']['user_id']='222'
        with self.assertRaises(ValueError): self.apply(message=m)
        self.assertEqual(self.store.projection(Subject('qq','111'),'self_private',Subject('qq','111'))['score'],0)

    def test_state_changed_while_evaluating(self):
        self.store.grant(Subject('qq','111'),4,authenticated_admin=self.admin)
        with self.assertRaises(VersionConflict): self.apply()
        state = self.store.projection(Subject('qq','111'),'self_private',Subject('qq','111'))
        self.assertEqual(state['manual_grant'],4)
        self.assertEqual(state['score'],0)

    def test_wrong_namespace(self):
        other = RelationStore(Path(self.tmp.name)/'other.sqlite3')
        with self.assertRaises(ValueError):
            apply_bound_result(other,self.receipt,self.message,self.binding,self.raw)

    def test_abstention(self):
        self.assertIsNone(self.apply(raw='{"kind":"none","delta":0}'))

    def test_invalid_receipt_and_model_authorization(self):
        for version in (-1, True, '1'):
            with self.assertRaises(ValueError): Receipt(self.receipt.subject,version)
        with self.assertRaises(ValueError):
            self.apply(raw='{"kind":"grant4","delta":1}')


if __name__ == '__main__': unittest.main()
