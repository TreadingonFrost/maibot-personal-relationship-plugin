from types import SimpleNamespace
import unittest
from interaction_control import ControlPhrases, InteractionState, match_control


def identity(uid='1000000001',sid='s',private=True):
    return SimpleNamespace(platform='qq',user_id=uid,session_id=sid,private=private)


class ControlTests(unittest.TestCase):
    def test_exact_custom(self):
        p=ControlPhrases('暂停贴贴','恢复贴贴')
        self.assertEqual(match_control('暂停贴贴',p,private=True),'stop')
        self.assertEqual(match_control('恢复贴贴',p,private=True),'resume')

    def test_no_false_substrings_quotes_or_group(self):
        p=ControlPhrases()
        for text in ('别说/关系暂停了','“/关系暂停”',' /关系暂停','/关系暂停。','我说的是/关系暂停','代码：/关系暂停'):
            self.assertIsNone(match_control(text,p,private=True))
        self.assertIsNone(match_control(p.stop,p,private=False))
        self.assertIsNone(match_control(p.stop,p,private=True,quoted=True))

    def test_invalid_controls(self):
        for a,b in (('停','恢复'),('same','same'),(' x','resume'),('a\nb','resume'),('/relation status','resume')):
            with self.assertRaises(ValueError): ControlPhrases(a,b)

    def test_subject_and_session_isolation(self):
        s=InteractionState(); a=identity(); s.control(a,'stop')
        self.assertIn(s.key(a),s.paused)
        self.assertNotIn(s.key(identity('222')),s.paused)
        self.assertNotIn(s.key(identity(sid='other')),s.paused)
        s.clear_previews(); self.assertIn(s.key(a),s.paused)
        s.control(a,'resume'); self.assertNotIn(s.key(a),s.paused)

    def test_preview_one_message_and_retries(self):
        s=InteractionState(); a=identity(); s.set_preview(a,-2,admin_uid="1000000001")
        self.assertEqual(s.preview_level(a,'m1'),-2)
        self.assertEqual(s.preview_level(a,'m1'),-2)
        self.assertIsNone(s.preview_level(a,'m2'))
        self.assertEqual(s.preview_level(a,'m1'),-2)

    def test_preview_unconfigured_or_changed_admin_rejected(self):
        state = InteractionState()
        for admin_uid in ('', '01000000001', '1000000003'):
            with self.assertRaises(PermissionError):
                state.set_preview(identity(), 5, admin_uid=admin_uid)

    def test_preview_admin_self_only_and_pause(self):
        s=InteractionState()
        for a in (identity('222'),identity(private=False)):
            with self.assertRaises(PermissionError): s.set_preview(a,5,admin_uid="1000000001")
        a=identity(); s.set_preview(a,4,admin_uid="1000000001"); s.control(a,'stop')
        self.assertIsNone(s.preview_level(a,'m'))
        with self.assertRaises(ValueError): s.set_preview(a,4,admin_uid="1000000001")
