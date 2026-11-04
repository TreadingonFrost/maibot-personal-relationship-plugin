"""Host-free WebUI operator tests; isolated package loading, temporary DB only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PKG = "isolated_webui_relation"
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(ROOT)]
sys.modules[PKG] = pkg
for name in ("relation_core", "admin_webui"):
    spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
core = sys.modules[f"{PKG}.relation_core"]
Backend = sys.modules[f"{PKG}.admin_webui"].AdminWebUIBackend


class AdminWebUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "relations.sqlite3"
        self.store = core.RelationStore(self.path, cooldown_seconds=0, daily_cap=100, admin_subjects=[core.Subject("qq", "1000000001")])
        self.backend = Backend(self.store, operator_enabled=True)
        self.qq = "123456789"

    def tearDown(self):
        self.tmp.cleanup()

    def audits(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM operator_audit ORDER BY id")]

    def test_unconfigured_admin_fails_closed_even_for_operator(self):
        store = core.RelationStore(Path(self.tmp.name) / 'unconfigured.sqlite3')
        backend = Backend(store, operator_enabled=True)
        for call in (lambda: backend.query(self.qq), lambda: backend.set_score(self.qq, 20, 0),
                     lambda: backend.grant(self.qq, 4, 0)):
            with self.assertRaises(PermissionError):
                call()

    def test_disabled_by_default(self):
        disabled = Backend(self.store)
        for call in (lambda: disabled.query(self.qq), lambda: disabled.set_score(self.qq, 20, 0), lambda: disabled.grant(self.qq, 4, 0)):
            with self.assertRaises(PermissionError):
                call()
        with self.assertRaises(ValueError):
            Backend(self.store, operator_enabled="true")

    def test_empty_target_never_enumerates_or_opens_database(self):
        original = self.store._connect
        def forbidden():
            raise AssertionError("empty query must not open DB")
        self.store._connect = forbidden
        try:
            result = self.backend.query()
        finally:
            self.store._connect = original
        self.assertEqual(result["target_qq"], "")
        self.assertIsNone(result["score"])

    def test_strict_ascii_qq_not_nickname(self):
        for value in ("a nickname", " 123456789", "1234", "012345", "１２３４５６", "12345;DROP", 123456789, None):
            with self.assertRaises(ValueError):
                self.backend.query(value)
        self.assertEqual(self.backend.query(self.qq)["score"], 0)

    def test_exact_query_does_not_persist_or_return_others(self):
        self.backend.set_score("987654321", -90, 0)
        result = self.backend.query(self.qq)
        self.assertEqual(result["score"], 0)
        self.assertNotIn("987654321", repr(result))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 1)

    def test_score_and_operator_audit_not_qq_impersonation(self):
        result = self.backend.set_score(self.qq, 42.5, 0)
        self.assertEqual((result["score"], result["version"]), (42.5, 1))
        row = self.audits()[0]
        self.assertEqual(row["executor_kind"], "host_webui_operator")
        self.assertEqual((row["old_score"], row["new_score"]), (0, 42.5))
        self.assertFalse({"admin_user_id", "admin_platform"} & row.keys())
        info = self.store.admin_inspect(core.Subject("qq", self.qq), authenticated_admin=core.Subject("qq", "1000000001"))
        self.assertEqual(info["score_audit"], [])
        self.assertEqual(info["grant_audit"], [])

    def test_score_invalid_no_write(self):
        for value in (float("nan"), float("inf"), -200.01, 200.01, True, "30"):
            with self.assertRaises(ValueError):
                self.backend.set_score(self.qq, value, 0)
        self.assertEqual(self.audits(), [])

    def test_score_endpoints_and_display_ranks_do_not_auto_grant(self):
        for index, (score, rank, auto) in enumerate(((-200, -2, -2), (29.99, 0, 0),
                                                    (30, 1, 1), (70, 2, 2),
                                                    (100, 3, 2), (150, 4, 2),
                                                    (200, 4, 2))):
            result = self.backend.set_score(self.qq, score, index)
            self.assertEqual((result["score"], result["score_rank"], result["auto_level"]),
                             (score, rank, auto))
            self.assertEqual(result["effective_level"], auto)
            self.assertEqual(result["manual_grant"], 0)
        granted = self.backend.grant(self.qq, 3, result["version"])
        self.assertEqual(granted["effective_level"], 3)
        self.assertEqual(granted["score_rank"], 4)

    def test_grant_three_four_revoke_preserve_score(self):
        self.backend.set_score(self.qq, -80, 0)
        for version, level in enumerate((3, 4, 0), 1):
            result = self.backend.grant(self.qq, level, version)
            self.assertEqual(result["manual_grant"], level)
            self.assertEqual(result["score"], -80)
        self.assertEqual(result["effective_level"], -2)
        self.assertEqual(len(self.audits()), 4)

    def test_grant_five_and_invalid_types_rejected(self):
        for value in (5, 1, 2, True, 3.0, "4"):
            with self.assertRaises(ValueError):
                self.backend.grant(self.qq, value, 0)
        self.assertEqual(self.audits(), [])

    def test_fixed_admin_never_demoted(self):
        qq = core.Subject("qq", "1000000001").user_id
        for value in (-200, -100, 0, 200):
            with self.assertRaises(ValueError):
                self.backend.set_score(qq, value, 0)
        self.assertEqual(self.audits(), [])
        before = self.backend.query(qq)
        self.assertEqual((before["score"], before["auto_level"], before["effective_level"]), (200, 2, 5))
        self.assertEqual(before["score_rank"], 4)
        self.assertEqual(self.backend.grant(qq, 0, before["version"])["effective_level"], 5)
        self.assertEqual(self.backend.query(qq)["score"], 200)

    def test_expected_version_required_and_stale_rollback(self):
        for version in (None, -1, True, 1.0, "0"):
            with self.assertRaises(ValueError):
                self.backend.set_score(self.qq, 20, version)
        self.backend.set_score(self.qq, 20, 0)
        with self.assertRaises(core.VersionConflict):
            self.backend.grant(self.qq, 4, 0)
        self.assertEqual(len(self.audits()), 1)
        self.assertEqual(self.backend.query(self.qq)["manual_grant"], 0)

    def test_concurrent_cas_has_one_audit(self):
        def work(value):
            try:
                return self.backend.set_score(self.qq, value, 0)
            except core.VersionConflict:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(work, range(20)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.audits()), 1)

    def test_preserve_automatic_budget_and_counters(self):
        subject = core.Subject("qq", self.qq)
        self.store.record_event(subject, "m1", "targeted_harassment", -5, source_user_id=self.qq)
        with closing(sqlite3.connect(self.path)) as db:
            before = db.execute("SELECT last_event,day,daily_points,daily_count FROM relations").fetchone()
        self.backend.set_score(self.qq, 90, 1)
        self.backend.grant(self.qq, 4, 2)
        with closing(sqlite3.connect(self.path)) as db:
            after = db.execute("SELECT last_event,day,daily_points,daily_count FROM relations").fetchone()
            count = db.execute("SELECT count FROM negative_adjustments").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(count, 1)
        self.assertEqual(self.store.record_event(subject, "m1", "meaningful_support", 5, source_user_id=self.qq).reason, "duplicate")

    def test_platform_isolation_and_public_no_operator_fields(self):
        self.store.admin_set_score(core.Subject("discord", self.qq), -100, authenticated_admin=core.Subject("qq", "1000000001"))
        self.backend.set_score(self.qq, 80, 0)
        self.backend.grant(self.qq, 4, 1)
        self.assertEqual(self.backend.query(self.qq)["score"], 80)
        public = self.store.projection(core.Subject("qq", self.qq), "public_group", core.Subject("qq", "other"))
        self.assertEqual(set(public), {"subject", "familiarity", "trust_summary", "effective_level"})
        self.assertEqual(public["effective_level"], 2)
        self.assertNotIn("executor_kind", self.backend.query(self.qq))

    def test_page_declares_only_three_private_api_names(self):
        declaration = json.loads((ROOT / "webui.json").read_text(encoding="utf-8"))
        self.assertEqual(declaration["schema_version"], 2)
        self.assertIn("declarative_basic", declaration["required_capabilities"])
        page = declaration["pages"][0]
        names = {binding["api"] for binding in [*page["queries"].values(), *page["actions"].values()]}
        self.assertEqual(names, {"relation_webui_query", "relation_webui_set_score", "relation_webui_grant"})
        for binding in page["actions"].values():
            self.assertTrue(binding["confirmation"])
            self.assertIn("expected_version", binding["parameters"])
            self.assertEqual(binding["arguments"]["target_qq"]["field"], "target_qq")
        self.assertFalse(page["auto_refresh"])
        score_schema = page["actions"]["set_score"]["parameters"]["score"]
        self.assertEqual((score_schema["minimum"], score_schema["maximum"]), (-200, 200))
        content_text = json.dumps(page["content"], ensure_ascii=False)
        self.assertIn("100/150 仅代表积分档 3/4", content_text)
        self.assertIn("自动关系等级最高 2", content_text)
        self.assertIn("实际 3/4 级仍须手动授权", content_text)
        self.assertIn('"field": "score_rank"', content_text)
        empty = self.backend.query()
        self.assertIsNone(empty["score_rank"])
        for key in ("grant3", "grant4", "revoke"):
            self.assertEqual(empty[key], {"grant3": 3, "grant4": 4, "revoke": 0}[key])


if __name__ == "__main__":
    unittest.main()
