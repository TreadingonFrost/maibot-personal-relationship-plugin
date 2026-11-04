"""Offline tests: only temporary SQLite databases, no Host or network."""
import concurrent.futures
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

# Load without importing a future Host plugin package's __init__.
SPEC = importlib.util.spec_from_file_location("isolated_relation_core", Path(__file__).resolve().parents[1] / "relation_core.py")
core = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = core
SPEC.loader.exec_module(core)
Subject, RelationStore, VersionConflict = core.Subject, core.RelationStore, core.VersionConflict


class RelationCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "relation.sqlite3"
        self.now = 1000000.0
        self.admin = Subject("qq", "1000000001")
        self.a, self.b = Subject("qq", "100"), Subject("qq", "200")
        self.store = RelationStore(self.path, admin_subjects=[self.admin], cooldown_seconds=0,
                                   daily_cap=100, max_delta=100, clock=lambda: self.now,
                                   negative_relation_factors=(1,) * 8, negative_daily_decay=1,
                                   thresholds=(20, 60))

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, who=None, mid="m1", delta=5, kind="meaningful_support", **kwargs):
        who = who or self.a
        return self.store.record_event(who, mid, kind, delta, source_user_id=who.user_id, **kwargs)

    def private(self, who=None):
        who = who or self.a
        return self.store.projection(who, "self_private", who)

    def grant(self, level, who=None, **kwargs):
        return self.store.grant(who or self.a, level, authenticated_admin=self.admin, **kwargs)

    def test_subject_strict(self):
        for bad in ("", " ", None, 0, False):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Subject("qq", bad)
            with self.assertRaises(ValueError):
                Subject(bad, "x")

    def test_subject_exact_not_normalized(self):
        self.assertNotEqual(Subject("qq", "01"), Subject("qq", "1"))
        self.assertNotEqual(Subject("QQ", "1"), Subject("qq", "1"))

    def test_default_state(self):
        self.assertEqual(self.private()["score"], 0)
        self.assertEqual(self.private()["effective_level"], 0)

    def test_cross_user(self):
        self.event(delta=70)
        self.assertEqual(self.private(self.b)["score"], 0)

    def test_cross_platform(self):
        other = Subject("discord", "100")
        self.event(delta=70)
        self.assertEqual(self.private(other)["score"], 0)
        self.assertTrue(self.event(other, delta=1).accepted)

    def test_thresholds_and_auto_cap(self):
        self.event(delta=19)
        self.assertEqual(self.private()["auto_level"], 0)
        self.event(mid="m2", delta=1)
        self.assertEqual(self.private()["auto_level"], 1)
        self.event(mid="m3", delta=80)
        self.assertEqual(self.private()["score"], 100)
        self.assertEqual(self.private()["auto_level"], 2)

    def test_configurable_thresholds(self):
        other = RelationStore(self.path, thresholds=(1, 2), cooldown_seconds=0)
        other.record_event(self.a, "x", "mutual_humor", 2, source_user_id="100")
        self.assertEqual(other.projection(self.a, "self_private", self.a)["auto_level"], 2)

    def test_grant_independent_of_score(self):
        state = self.grant(4)
        self.assertEqual((state.score, state.auto_level, state.effective_level), (0, 0, 4))
        self.assertFalse(hasattr(self.store, "set_score"))

    def test_revoke_preserves_signals(self):
        self.event(delta=70)
        self.grant(4)
        self.assertEqual(self.grant(0).effective_level, 2)

    def test_unauthorized_grants(self):
        for actor in (self.a, Subject("other", "root")):
            with self.assertRaises(PermissionError):
                self.store.grant(self.a, 4, authenticated_admin=actor)
        self.assertEqual(self.private()["version"], 0)

    def test_exact_subject_no_nickname(self):
        with self.assertRaises(ValueError):
            self.store.grant("nickname", 3, authenticated_admin=self.admin)
        with self.assertRaises(ValueError):
            self.store.grant(self.a, 3, authenticated_admin="root")

    def test_invalid_manual_levels(self):
        for value in (True, 1, 2, 5, "4", 3.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.grant(value)

    def test_dedup_subject_and_message(self):
        self.assertTrue(self.event().accepted)
        result = self.event(delta=50)
        self.assertEqual(result.reason, "duplicate")
        self.assertEqual(result.state.score, 5)
        self.assertTrue(self.event(self.b).accepted)

    def test_source_must_match(self):
        with self.assertRaises(ValueError):
            self.store.record_event(self.a, "x", "shared_activity", 2, source_user_id="200")
        self.assertEqual(self.private()["score"], 0)

    def test_message_counts_never_award(self):
        for i in range(100):
            self.assertFalse(self.event(mid=str(i), kind="message", delta=100).accepted)
        self.assertEqual(self.private()["score"], 0)

    def test_only_explicit_event_kinds(self):
        for kind in ("set_score", "chat", "unknown", "grant", [], None):
            with self.assertRaises(ValueError):
                self.event(kind=kind)
        for i, kind in enumerate(sorted(self.store.EVENT_KINDS)):
            self.assertTrue(self.event(mid=str(i), kind=kind).accepted)

    def test_invalid_numbers(self):
        for number in (float("nan"), float("inf"), -float("inf"), -1, True, "4", 10**10000):
            with self.assertRaises(ValueError):
                self.event(delta=number)
        self.assertEqual(self.private()["score"], 0)

    def test_bounded_delta(self):
        store = RelationStore(self.path, max_delta=3, cooldown_seconds=0)
        result = store.record_event(self.a, "x", "shared_activity", 100, source_user_id="100")
        self.assertEqual(result.state.score, 3)

    def test_cooldown_and_consumed_rejection(self):
        self.store.cooldown_seconds = 60
        self.event()
        self.assertEqual(self.event(mid="m2").reason, "cooldown")
        self.now += 60
        self.assertEqual(self.event(mid="m2").reason, "duplicate")
        self.assertTrue(self.event(mid="m3").accepted)

    def test_daily_points_limit_and_reset(self):
        self.store.daily_cap = 7
        self.event()
        self.assertEqual(self.event(mid="m2").state.score, 7)
        self.assertEqual(self.event(mid="m3").reason, "daily_limit")
        self.now += 86400
        self.assertTrue(self.event(mid="m4").accepted)

    def test_daily_event_limit(self):
        self.store.daily_event_limit = 1
        self.event()
        self.assertEqual(self.event(mid="m2").reason, "daily_limit")
        self.assertTrue(self.event(self.b).accepted)

    def test_public_allowlist_and_grant_non_disclosure(self):
        before = self.store.projection(self.a, "public_group", self.b)
        self.grant(4)
        after = self.store.projection(self.a, "public_group", self.b)
        self.assertEqual(before, after)
        self.assertEqual(set(after), {"subject", "familiarity", "trust_summary", "effective_level"})
        self.event(delta=70)
        public = self.store.projection(self.a, "public_group", self.b)
        self.assertEqual(public["effective_level"], 2)
        self.assertEqual(public["familiarity"], "close")

    def test_private_only_self_even_for_admin(self):
        for viewer in (self.b, self.admin, Subject("discord", "100")):
            with self.assertRaises(PermissionError):
                self.store.projection(self.a, "self_private", viewer)

    def test_unknown_audience_rejected(self):
        with self.assertRaises(ValueError):
            self.store.projection(self.a, "admin", self.admin)

    def test_admin_targeted_inspection(self):
        self.grant(3)
        self.grant(4, self.b)
        with self.assertRaises(PermissionError):
            self.store.admin_inspect(self.a, authenticated_admin=self.b)
        info = self.store.admin_inspect(self.a, authenticated_admin=self.admin)
        self.assertEqual(len(info["grant_audit"]), 1)
        self.assertEqual(info["grant_audit"][0]["new_level"], 3)
        self.assertNotIn('200', repr(info))

    def test_grant_audit_and_optimistic_version(self):
        self.grant(3, expected_version=0)
        with self.assertRaises(VersionConflict):
            self.grant(4, expected_version=0)
        info = self.store.admin_inspect(self.a, authenticated_admin=self.admin)
        self.assertEqual(info["version"], 1)
        self.assertEqual(len(info["grant_audit"]), 1)
        self.assertEqual(self.grant(0, expected_version=1).version, 2)

    def test_event_version_rollback_does_not_consume(self):
        self.grant(3)
        with self.assertRaises(VersionConflict):
            self.event(expected_version=0)
        self.assertTrue(self.event(expected_version=1).accepted)
        self.assertEqual(self.private()["manual_grant"], 3)

    def test_concurrent_duplicates(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.event(), range(30)))
        self.assertEqual(sum(r.accepted for r in results), 1)
        self.assertEqual(self.private()["score"], 5)

    def test_concurrent_distinct_no_lost_updates(self):
        self.store.daily_event_limit = 100
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.event(mid=str(i), delta=1), range(40)))
        self.assertTrue(all(r.accepted for r in results))
        self.assertEqual(self.private()["score"], 40)
        self.assertEqual(self.private()["version"], 40)

    def test_concurrent_grant_conflict(self):
        def work(level):
            try:
                return self.grant(level, expected_version=0)
            except VersionConflict:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(work, [3, 4]))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(len(self.store.admin_inspect(self.a, authenticated_admin=self.admin)["grant_audit"]), 1)

    def test_grants_and_signals_concurrent_no_overwrite(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.grant, 4)
            b = pool.submit(self.event, delta=30)
            a.result(), b.result()
        self.assertEqual((self.private()["score"], self.private()["manual_grant"], self.private()["version"]), (30, 4, 2))

    def test_schema_no_free_text_notes_or_body(self):
        self.event()
        self.grant(4)
        with closing(sqlite3.connect(self.path)) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            cols = {r[1] for table in tables for r in db.execute(f"PRAGMA table_info({table})")}
        self.assertFalse(cols & {"body", "content", "notes", "nickname", "prompt", "evidence_text"})
        with self.assertRaises(TypeError):
            self.store.record_event(self.a, "x", "shared_activity", 2, source_user_id="100", notes="private")

    def test_separate_store_namespace(self):
        self.grant(4)
        second = RelationStore(Path(self.tmp.name) / "other-bot.sqlite3", admin_subjects=[self.admin])
        self.assertEqual(second.projection(self.a, "self_private", self.a)["manual_grant"], 0)

    def test_restart_persists_and_connections_are_short(self):
        self.event(delta=3)
        reopened = RelationStore(self.path)
        self.assertEqual(reopened.projection(self.a, "self_private", self.a)["score"], 3)
        self.assertFalse(any(isinstance(x, sqlite3.Connection) for x in vars(self.store).values()))

    def test_configurable_single_admin_and_empty_failclosed(self):
        self.assertFalse(hasattr(core, "GLOBAL_ADMIN"))
        for bad in (Subject("discord", "1000000001"), Subject("QQ", "1000000001"), Subject("qq", "nickname")):
            with self.assertRaises(ValueError):
                RelationStore(self.path, admin_subjects=[bad])
        for admins in ([self.admin, self.a], [self.admin, self.admin]):
            with self.assertRaises(ValueError):
                RelationStore(self.path, admin_subjects=admins)
        empty = RelationStore(self.path)
        self.assertEqual(empty.admin_subjects, frozenset())
        self.assertIsNone(empty.admin_subject)
        self.assertEqual(empty.projection(self.admin, "self_private", self.admin)["effective_level"], 0)
        with self.assertRaises(PermissionError):
            empty.grant(self.a, 3, authenticated_admin=self.admin)
        replacement = RelationStore(self.path, admin_subjects=[self.b])
        self.assertTrue(replacement.is_admin(self.b))
        self.assertFalse(replacement.is_admin(self.admin))
        self.assertEqual(replacement.grant(self.a, 3, authenticated_admin=self.b).manual_grant, 3)
        self.assertEqual(core.RelationState(self.admin).effective_level, 0)

    def test_admin_identity_not_mutable_configuration(self):
        with self.assertRaises(AttributeError):
            self.store.admin_subjects = frozenset({self.b})
        with self.assertRaises(PermissionError):
            self.store.grant(self.a, 4, authenticated_admin=self.b)
        self.assertEqual(self.grant(4).manual_grant, 4)

    def test_admin_identity_cannot_be_demoted(self):
        self.assertEqual(self.private(self.admin)["effective_level"], 5)
        for level in (0, 3, 4):
            self.assertEqual(self.grant(level, self.admin).effective_level, 5)
        state = self.event(self.admin, delta=-100, kind="targeted_harassment").state
        self.assertEqual((state.score, state.auto_level, state.effective_level), (200, 2, 5))
        public = self.store.projection(self.admin, "public_group", self.a)
        self.assertEqual((public["effective_level"], public["familiarity"]), (2, "close"))
        self.assertEqual(set(public), {"subject", "familiarity", "trust_summary", "effective_level"})
        for platform in ("QQ", "discord"):
            self.assertEqual(self.private(Subject(platform, "1000000001"))["effective_level"], 0)

    def test_grade_five_never_grantable(self):
        for target in (self.a, self.admin):
            with self.assertRaises(ValueError):
                self.grant(5, target)
        self.assertEqual(self.private()["version"], 0)

    def test_negative_thresholds_and_recovery(self):
        self.event(delta=-19, kind="targeted_harassment")
        self.assertEqual(self.private()["effective_level"], 0)
        self.event(mid="n2", delta=-1, kind="repeated_boundary_violation")
        self.assertEqual(self.private()["effective_level"], -1)
        self.event(mid="n3", delta=-40, kind="targeted_harassment")
        self.assertEqual(self.private()["effective_level"], -2)
        self.now += 86400
        self.event(mid="recovery", delta=100)
        self.assertEqual((self.private()["score"], self.private()["effective_level"]), (40, 1))

    def test_negative_score_floor_and_public_summary(self):
        self.event(delta=-200, kind="targeted_harassment")
        self.assertEqual(self.private()["score"], -100)
        public = self.store.projection(self.a, "public_group", self.b)
        self.assertEqual(public["effective_level"], -2)
        self.assertEqual(public["familiarity"], "boundaries_required")
        self.grant(4)
        self.assertEqual(self.store.projection(self.a, "public_group", self.b), public)
        self.assertEqual(self.private()["effective_level"], 4)
        self.assertEqual(self.grant(0).effective_level, -2)

    def test_negative_evidence_requires_negative_delta(self):
        for kind in self.store.NEGATIVE_EVENT_KINDS:
            for delta in (0, 1, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    self.event(kind=kind, delta=delta)
        self.assertEqual(self.private()["score"], 0)

    def test_ordinary_teasing_profanity_not_event_kinds(self):
        for kind in ("teasing", "profanity", "single_swear", "banter"):
            with self.assertRaises(ValueError):
                self.event(kind=kind, delta=-5)
        self.assertFalse(self.event(kind="message", delta=-100).accepted)
        self.assertEqual(self.private()["score"], 0)

    def test_absolute_budget_no_refund_on_reverse(self):
        self.store.daily_cap = 10
        self.event(delta=6)
        self.assertEqual(self.event(mid="negative", delta=-6, kind="targeted_harassment").state.score, 2)
        self.assertEqual(self.event(mid="again", delta=10).reason, "daily_limit")
        self.now += 86400
        self.assertTrue(self.event(mid="next", delta=-10, kind="targeted_harassment").accepted)

    def test_negative_cooldown_dedup_and_source(self):
        self.store.cooldown_seconds = 60
        self.event(delta=-5, kind="targeted_harassment")
        self.assertEqual(self.event(mid="n2", delta=-5, kind="targeted_harassment").reason, "cooldown")
        self.now += 60
        self.assertEqual(self.event(mid="n2", delta=-5, kind="targeted_harassment").reason, "duplicate")
        with self.assertRaises(ValueError):
            self.store.record_event(self.a, "bad", "targeted_harassment", -5, source_user_id=self.b.user_id)

    def test_concurrent_signed_absolute_budget(self):
        self.store.daily_cap = 17
        self.store.daily_event_limit = 100
        def work(i):
            return self.event(mid=str(i), delta=1 if i % 2 else -1,
                              kind="meaningful_support" if i % 2 else "targeted_harassment")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(work, range(50)))
        self.assertEqual(sum(r.accepted for r in results), 17)
        with closing(sqlite3.connect(self.path)) as db:
            points, count = db.execute("SELECT daily_points,daily_count FROM relations WHERE platform='qq' AND user_id='100'").fetchone()
            total = db.execute("SELECT SUM(ABS(awarded)) FROM inbound").fetchone()[0]
        self.assertEqual((points, count, total), (17, 17, 17))
        self.assertLessEqual(abs(self.private()["score"]), 17)

    def test_old_unsigned_schema_rejected_without_migration(self):
        old_path = Path(self.tmp.name) / "old.sqlite3"
        with closing(sqlite3.connect(old_path)) as db:
            db.execute("CREATE TABLE relations(score REAL CHECK(score >= 0 AND score <= 100))")
            db.commit()
        before = old_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "offline migration"):
            RelationStore(old_path)
        self.assertEqual(old_path.read_bytes(), before)

    def test_negative_state_cross_user_platform_isolation(self):
        self.event(delta=-90, kind="targeted_harassment")
        self.assertEqual(self.private(self.b)["score"], 0)
        self.assertEqual(self.private(Subject("discord", "100"))["score"], 0)
        for viewer in (self.b, self.admin):
            with self.assertRaises(PermissionError):
                self.store.projection(self.a, "self_private", viewer)

    def damped_store(self, **kwargs):
        options = dict(admin_subjects=[self.admin], cooldown_seconds=0, daily_cap=100,
                       max_delta=100, daily_event_limit=100, clock=lambda: self.now)
        options.update(kwargs)
        self.store = RelationStore(self.path, **options)
        return self.store

    def negative_count(self, subject=None, day=None):
        subject = subject or self.a
        day = int(self.now // 86400) if day is None else day
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT count FROM negative_adjustments WHERE platform=? AND user_id=? AND day=?",
                             (subject.platform, subject.user_id, day)).fetchone()
        return row[0] if row else 0

    def test_damping_default_daily_formula_and_floor(self):
        self.damped_store()
        actual = []
        previous = 0
        for i in range(8):
            result = self.event(mid=str(i), delta=-2, kind="repeated_boundary_violation")
            actual.append(previous - result.state.score)
            previous = result.state.score
        for change, expected in zip(actual, (2, 2, 2, 1, .5, .25, .2, .2)):
            self.assertAlmostEqual(change, expected)
        self.assertEqual(self.negative_count(), 8)

    def test_damping_relation_factors_cover_all_effective_levels(self):
        for level, expected_factor in zip(range(-2, 6), (1, 1, 1, .9, .8, .7, .6, .6)):
            with self.subTest(level=level):
                # Each case has a separate namespace/database, not direct score writes.
                path = Path(self.tmp.name) / f"level_{level}.sqlite3"
                store = RelationStore(path, admin_subjects=[self.admin], cooldown_seconds=0, daily_cap=100, max_delta=100, clock=lambda: self.now)
                subject = self.admin if level == 5 else self.a
                if level in (-2, -1, 1, 2):
                    score = {-2: -70, -1: -30, 1: 30, 2: 70}[level]
                    store.record_event(subject, "seed", "targeted_harassment" if score < 0 else "meaningful_support", score, source_user_id=subject.user_id)
                if level in (3, 4):
                    store.grant(subject, level, authenticated_admin=self.admin)
                before = store.projection(subject, "self_private", subject)["score"]
                result = store.record_event(subject, "test", "repeated_boundary_violation", -10, source_user_id=subject.user_id)
                self.assertAlmostEqual(before - result.state.score, 0 if level == 5 else 10 * expected_factor)
                if level == 5:
                    self.assertEqual(result.reason, 'fixed_admin_score')

    def test_damping_combines_relation_daily_before_caps(self):
        self.damped_store(negative_daily_threshold=1, max_delta=5)
        self.grant(4)
        self.event(mid="first", delta=-1, kind="repeated_boundary_violation")
        before = self.private()["score"]
        result = self.event(mid="second", delta=-20, kind="repeated_boundary_violation")
        # Raw 20 * relation .6 * daily .5 = 6, then max_delta => 5.
        self.assertAlmostEqual(before - result.state.score, 5)
        before = result.state.score
        result = self.event(mid="third", delta=-10, kind="repeated_boundary_violation")
        self.assertAlmostEqual(before - result.state.score, 1.5)
        self.assertEqual(result.state.manual_grant, 4)

    def test_damping_severe_bypasses_both_but_counts(self):
        self.damped_store(negative_daily_threshold=1)
        self.grant(4)
        for i in range(5):
            before = self.private()["score"]
            state = self.event(mid=str(i), delta=-2, kind="targeted_harassment").state
            self.assertAlmostEqual(before - state.score, 2)
        self.assertEqual(self.negative_count(), 5)
        before = self.private()["score"]
        state = self.event(mid="ordinary", delta=-2, kind="repeated_boundary_violation").state
        self.assertAlmostEqual(before - state.score, .12)  # .6 * floor .1 * 2

    def test_damping_utc_reset_and_positive_does_not_reset(self):
        self.damped_store(negative_daily_threshold=1)
        self.event(mid="n1", delta=-2, kind="repeated_boundary_violation")
        self.event(mid="positive", delta=1)
        before = self.private()["score"]
        state = self.event(mid="n2", delta=-2, kind="repeated_boundary_violation").state
        self.assertAlmostEqual(before - state.score, 1)
        old_day = int(self.now // 86400)
        self.now = (old_day + 1) * 86400
        before = state.score
        state = self.event(mid="next", delta=-2, kind="repeated_boundary_violation").state
        self.assertAlmostEqual(before - state.score, 2)
        self.assertEqual(self.negative_count(), 1)
        self.assertEqual(self.negative_count(day=old_day), 2)

    def test_damping_counter_rejections_and_version_rollback(self):
        self.damped_store(cooldown_seconds=60)
        self.event(mid="n1", delta=-2, kind="repeated_boundary_violation")
        self.assertEqual(self.event(mid="n1", delta=-2, kind="repeated_boundary_violation").reason, "duplicate")
        self.assertEqual(self.event(mid="n2", delta=-2, kind="repeated_boundary_violation").reason, "cooldown")
        self.now += 60
        with self.assertRaises(VersionConflict):
            self.event(mid="stale", delta=-2, kind="repeated_boundary_violation", expected_version=0)
        self.assertEqual(self.negative_count(), 1)
        self.assertTrue(self.event(mid="stale", delta=-2, kind="repeated_boundary_violation", expected_version=1).accepted)
        self.assertEqual(self.negative_count(), 2)

    def test_damping_budget_is_actual_absolute_change(self):
        self.damped_store(daily_cap=3, negative_daily_threshold=1)
        self.grant(4)
        self.event(mid="first", delta=-2, kind="repeated_boundary_violation")  # 1.2
        self.event(mid="second", delta=-2, kind="repeated_boundary_violation")  # .6
        self.event(mid="severe", delta=-100, kind="targeted_harassment")  # remaining 1.2
        self.assertAlmostEqual(self.private()["score"], -3)
        self.assertEqual(self.event(mid="excess", delta=-2, kind="repeated_boundary_violation").reason, "daily_limit")
        self.assertEqual(self.negative_count(), 3)

    def test_damping_concurrent_ordinals_no_lost_count(self):
        self.damped_store()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.event(mid=str(i), delta=-1, kind="repeated_boundary_violation"), range(20)))
        self.assertTrue(all(result.accepted for result in results))
        expected = sum(max(.1, .5 ** max(0, ordinal - 3)) for ordinal in range(1, 21))
        self.assertAlmostEqual(self.private()["score"], -expected)
        self.assertEqual(self.negative_count(), 20)
        self.assertEqual(self.private()["version"], 20)

    def test_damping_concurrent_duplicate_and_subject_platform_isolation(self):
        self.damped_store(negative_daily_threshold=1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.event(delta=-1, kind="repeated_boundary_violation"), range(20)))
        self.assertEqual(sum(result.accepted for result in results), 1)
        for subject in (self.b, Subject("discord", "100")):
            state = self.event(subject, delta=-1, kind="repeated_boundary_violation").state
            self.assertEqual(state.score, -1)
            self.assertEqual(self.negative_count(subject), 1)
        self.assertEqual(self.negative_count(), 1)

    def test_damping_disable_and_reopen_persist_count(self):
        self.damped_store(negative_relation_factors=(1,) * 8, negative_daily_decay=1)
        self.grant(4)
        for i in range(6):
            self.event(mid=str(i), delta=-1, kind="repeated_boundary_violation")
        self.assertEqual(self.private()["score"], -6)
        self.damped_store()  # Existing signed schema safely reopens.
        before = self.private()["score"]
        state = self.event(mid="reopened", delta=-1, kind="repeated_boundary_violation").state
        self.assertAlmostEqual(before - state.score, .06)
        self.assertEqual(self.negative_count(), 7)
        public = self.store.projection(self.a, "public_group", self.b)
        self.assertEqual(set(public), {"subject", "familiarity", "trust_summary", "effective_level"})
        self.assertNotIn("negative_count", self.private())

    def test_damping_configuration_strict(self):
        invalid = (0, -1, 1.01, float("nan"), float("inf"), True, "0.5", None)
        for value in invalid:
            for key in ("negative_daily_decay", "negative_min_factor"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.damped_store(**{key: value})
            with self.assertRaises(ValueError):
                self.damped_store(negative_relation_factors=(1, 1, 1, value, 1, 1, 1, 1))
        for value in (0, -1, True, 1.0, "3", None):
            with self.assertRaises(ValueError):
                self.damped_store(negative_daily_threshold=value)
        for value in ((1,) * 7, (1,) * 9, [1] * 8, None):
            with self.assertRaises(ValueError):
                self.damped_store(negative_relation_factors=value)

    def test_damping_additive_table_preserves_signed_schema(self):
        self.event(delta=7)
        with closing(sqlite3.connect(self.path)) as db:
            original = db.execute("SELECT sql FROM sqlite_master WHERE name='relations'").fetchone()[0]
            db.execute("DROP TABLE negative_adjustments")
            db.commit()
        self.damped_store()
        self.assertEqual(self.private()["score"], 7)
        with closing(sqlite3.connect(self.path)) as db:
            current = db.execute("SELECT sql FROM sqlite_master WHERE name='relations'").fetchone()[0]
        self.assertEqual(original, current)
        self.assertEqual(self.negative_count(), 0)

    def test_admin_set_score_exact_and_audit(self):
        state = self.store.admin_set_score(self.a, -25.5, authenticated_admin=self.admin, expected_version=0)
        self.assertEqual((state.score, state.auto_level, state.version), (-25.5, -1, 1))
        same = self.store.admin_set_score(self.a, -25.5, authenticated_admin=self.admin, expected_version=1)
        self.assertEqual(same.version, 2)
        audit = self.store.admin_inspect(self.a, authenticated_admin=self.admin)["score_audit"]
        self.assertEqual(len(audit), 2)
        self.assertEqual((audit[0]["old_score"], audit[0]["new_score"], audit[0]["version"]), (0, -25.5, 1))
        self.assertEqual((audit[0]["admin_platform"], audit[0]["admin_user_id"]), ("qq", "1000000001"))

    def test_admin_set_score_unauthorized_and_exact_subject(self):
        for actor in (self.a, Subject("discord", "1000000001"), Subject("qq", "01000000001")):
            with self.assertRaises(PermissionError):
                self.store.admin_set_score(self.a, 100, authenticated_admin=actor)
        with self.assertRaises(ValueError):
            self.store.admin_set_score("nickname", 100, authenticated_admin=self.admin)
        self.assertEqual(self.private()["version"], 0)
        self.assertEqual(self.store.admin_inspect(self.a, authenticated_admin=self.admin)["score_audit"], [])

    def test_admin_set_score_invalid_finite_range(self):
        for value in (float("nan"), float("inf"), -float("inf"), True, "42", None, -200.01, 200.01, 10**10000):
            with self.assertRaises(ValueError):
                self.store.admin_set_score(self.a, value, authenticated_admin=self.admin)
        self.assertEqual(self.private()["version"], 0)
        for value in (-100, 100):
            self.assertEqual(self.store.admin_set_score(self.a, value, authenticated_admin=self.admin).score, value)

    def test_admin_set_score_cas_atomic_audit(self):
        self.event()
        with self.assertRaises(VersionConflict):
            self.store.admin_set_score(self.a, -50, authenticated_admin=self.admin, expected_version=0)
        self.assertEqual(self.private()["score"], 5)
        self.assertEqual(self.store.admin_inspect(self.a, authenticated_admin=self.admin)["score_audit"], [])
        def work(score):
            try:
                return self.store.admin_set_score(self.a, score, authenticated_admin=self.admin, expected_version=1)
            except VersionConflict:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(work, [-40, 80]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.store.admin_inspect(self.a, authenticated_admin=self.admin)["score_audit"]), 1)
        self.assertEqual(self.private()["version"], 2)

    def test_admin_set_score_preserves_manual_grant_and_admin_five(self):
        self.grant(4)
        state = self.store.admin_set_score(self.a, -100, authenticated_admin=self.admin)
        self.assertEqual((state.manual_grant, state.effective_level), (4, 4))
        with self.assertRaisesRegex(ValueError, "fixed at 200"):
            self.store.admin_set_score(self.admin, -100, authenticated_admin=self.admin)
        self.assertEqual(self.private(self.admin)["score"], 200)
        self.assertEqual(self.store.projection(self.admin, "public_group", self.b)["effective_level"], 2)

    def test_admin_set_score_budget_cooldown_negative_counts_preserved(self):
        self.store.daily_cap = 5
        self.store.cooldown_seconds = 60
        self.event(delta=-5, kind="targeted_harassment")
        with closing(sqlite3.connect(self.path)) as db:
            before = db.execute("SELECT last_event,day,daily_points,daily_count FROM relations WHERE platform='qq' AND user_id='100'").fetchone()
        state = self.store.admin_set_score(self.a, 80, authenticated_admin=self.admin)
        self.assertEqual(state.score, 80)
        with closing(sqlite3.connect(self.path)) as db:
            after = db.execute("SELECT last_event,day,daily_points,daily_count FROM relations WHERE platform='qq' AND user_id='100'").fetchone()
        self.assertEqual(before, after)
        self.assertEqual(self.negative_count(), 1)
        self.assertEqual(self.event().reason, "duplicate")
        self.assertEqual(self.event(mid="cooldown").reason, "cooldown")
        self.now += 60
        self.assertEqual(self.event(mid="limited").reason, "daily_limit")

    def test_admin_set_score_new_subject_uses_no_automatic_budget(self):
        self.store.daily_cap = 5
        state = self.store.admin_set_score(self.a, 80, authenticated_admin=self.admin)
        self.assertEqual(state.version, 1)
        self.assertEqual(self.negative_count(), 0)
        result = self.event(delta=5)
        self.assertTrue(result.accepted)
        self.assertEqual(result.state.score, 85)
        with closing(sqlite3.connect(self.path)) as db:
            points, count = db.execute("SELECT daily_points,daily_count FROM relations WHERE platform='qq' AND user_id='100'").fetchone()
        self.assertEqual((points, count), (5, 1))

    def test_admin_set_score_targeted_audit_privacy(self):
        self.store.admin_set_score(self.a, 42, authenticated_admin=self.admin)
        self.store.admin_set_score(self.b, -99, authenticated_admin=self.admin)
        self.store.admin_set_score(Subject("discord", "100"), -88, authenticated_admin=self.admin)
        info = self.store.admin_inspect(self.a, authenticated_admin=self.admin)
        self.assertEqual(len(info["score_audit"]), 1)
        self.assertEqual(info["score_audit"][0]["new_score"], 42)
        for audience, viewer in (("self_private", self.a), ("public_group", self.b)):
            projection = self.store.projection(self.a, audience, viewer)
            for key in ("score_audit", "grant_audit", "admin_user_id", "executor", "old_score", "new_score"):
                self.assertNotIn(key, projection)
        self.assertEqual(self.store.admin_inspect(Subject("qq", "unknown"), authenticated_admin=self.admin)["score_audit"], [])

    def test_group_high_levels_default_compatible_and_toggle(self):
        for grant in (3, 4):
            self.grant(grant)
            default = self.store.projection(self.a, "public_group", self.b)
            disabled = self.store.projection(self.a, "public_group", self.b, allow_group_high_levels=False)
            enabled = self.store.projection(self.a, "public_group", self.b, allow_group_high_levels=True)
            self.assertEqual(default, disabled)
            self.assertEqual(disabled["effective_level"], 0)
            self.assertEqual(enabled["effective_level"], grant)
            self.assertEqual(enabled["familiarity"], "close")
            self.assertEqual(self.store.projection(self.a, "public_group", self.b), default)

    def test_group_high_levels_admin_five_only_when_enabled(self):
        default = self.store.projection(self.admin, "public_group", self.b)
        enabled = self.store.projection(self.admin, "public_group", self.b, allow_group_high_levels=True)
        self.assertEqual(default["effective_level"], 2)
        self.assertEqual(enabled["effective_level"], 5)
        # Enabling changes only the exposed effective level for the fixed admin.
        self.assertEqual({k: v for k, v in default.items() if k != "effective_level"},
                         {k: v for k, v in enabled.items() if k != "effective_level"})
        spoof = Subject("discord", self.admin.user_id)
        self.assertEqual(self.store.projection(spoof, "public_group", self.b, allow_group_high_levels=True)["effective_level"], 0)

    def test_group_high_levels_no_automatic_authorization(self):
        for value in (-100, -20, 0, 20, 100):
            self.store.admin_set_score(self.a, value, authenticated_admin=self.admin)
            before = self.private()
            default = self.store.projection(self.a, "public_group", self.b)
            enabled = self.store.projection(self.a, "public_group", self.b, allow_group_high_levels=True)
            self.assertEqual(enabled, default)
            self.assertEqual(self.private(), before)
            self.assertEqual(before["manual_grant"], 0)

    def test_group_high_levels_negative_score_manual_and_revoke(self):
        self.store.admin_set_score(self.a, -100, authenticated_admin=self.admin)
        self.grant(4)
        self.assertEqual(self.store.projection(self.a, "public_group", self.b)["effective_level"], -2)
        self.assertEqual(self.store.projection(self.a, "public_group", self.b, allow_group_high_levels=True)["effective_level"], 4)
        self.grant(0)
        self.assertEqual(self.store.projection(self.a, "public_group", self.b, allow_group_high_levels=True)["effective_level"], -2)

    def test_group_high_levels_public_exact_allowlist(self):
        self.store.admin_set_score(self.a, -80, authenticated_admin=self.admin)
        self.grant(4)
        for subject in (self.a, self.admin):
            result = self.store.projection(subject, "public_group", self.b, allow_group_high_levels=True)
            self.assertEqual(set(result), {"subject", "familiarity", "trust_summary", "effective_level"})
            self.assertEqual(set(result["subject"]), {"platform", "user_id"})
            self.assertEqual(result["trust_summary"], "established_shared_trust")
            for forbidden in ("manual_grant", "score", "auto_level", "version", "grant_audit", "score_audit", "source_user_id", "admin_user_id", "executor_kind", "private_notes"):
                self.assertNotIn(forbidden, result)
            self.assertNotIn("200", repr(result))

    def test_group_high_levels_does_not_change_private_policy(self):
        self.grant(3)
        private = self.private()
        self.assertEqual(self.store.projection(self.a, "self_private", self.a, allow_group_high_levels=True), private)
        for viewer in (self.b, self.admin):
            with self.assertRaises(PermissionError):
                self.store.projection(self.a, "self_private", viewer, allow_group_high_levels=True)

    def test_group_high_levels_strict_bool_keyword_only(self):
        for invalid in (None, 0, 1, "true", [], {}):
            with self.assertRaises(ValueError):
                self.store.projection(self.a, "public_group", self.b, allow_group_high_levels=invalid)
        with self.assertRaises(TypeError):
            self.store.projection(self.a, "public_group", self.b, True)

    def test_new_default_thresholds_rank_not_authorization(self):
        current = RelationStore(self.path, admin_subjects=[self.admin])
        for score, rank, auto in ((29,0,0),(30,1,1),(69,1,1),(70,2,2),(100,3,2),(150,4,2),(200,4,2),(-29,0,0),(-30,-1,-1),(-70,-2,-2),(-200,-2,-2)):
            state = current.admin_set_score(self.a, score, authenticated_admin=self.admin)
            self.assertEqual((state.score_rank, state.auto_level, state.effective_level), (rank, auto, auto))
        current.grant(self.a, 3, authenticated_admin=self.admin)
        self.assertEqual(current.admin_set_score(self.a, 150, authenticated_admin=self.admin).effective_level, 3)

    def test_new_range_event_caps_and_admin_immutable(self):
        self.store.daily_cap = 1000
        self.store.max_delta = 1000
        self.assertEqual(self.event(delta=500).state.score, 200)
        self.assertEqual(self.event(mid="negative",delta=-1000,kind="targeted_harassment").state.score,-200)
        result = self.event(self.admin,mid="admin",delta=-100,kind="targeted_harassment")
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason,"fixed_admin_score")
        self.assertEqual(result.state.score,200)
        self.assertEqual(self.store.admin_inspect(self.admin,authenticated_admin=self.admin)["score"],200)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM relations WHERE user_id=?",(self.admin.user_id,)).fetchone()[0],0)

    def old_signed_database(self):
        old = Path(self.tmp.name) / "signed_old.sqlite3"
        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(old)) as target:
            source.backup(target)
        with closing(sqlite3.connect(old)) as db:
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='relations'").fetchone()[0]
            db.execute("DROP TABLE relations")
            db.execute(sql.replace('-200','-100').replace('<= 200','<= 100'))
            db.execute("INSERT INTO relations VALUES ('qq','100',75,4,9,123,44,8,2)")
            db.execute("INSERT INTO inbound VALUES ('qq','100','evidence','shared_activity','100',5,123)")
            db.commit()
        return old

    def test_explicit_migration_preserves_rows_and_backup(self):
        old = self.old_signed_database()
        backup = Path(self.tmp.name) / "backup.sqlite3"
        with self.assertRaisesRegex(ValueError,"offline migration"):
            RelationStore(old)
        core.migrate_score_range(old,backup)
        with closing(sqlite3.connect(old)) as db, closing(sqlite3.connect(backup)) as prior:
            self.assertEqual(db.execute("SELECT * FROM relations").fetchall(),prior.execute("SELECT * FROM relations").fetchall())
            self.assertEqual(db.execute("SELECT * FROM inbound").fetchall(),prior.execute("SELECT * FROM inbound").fetchall())
            self.assertIn('-100',prior.execute("SELECT sql FROM sqlite_master WHERE name='relations'").fetchone()[0])
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],'ok')
        migrated = RelationStore(old, admin_subjects=[self.admin])
        state = migrated.admin_set_score(self.a,150,authenticated_admin=self.admin,expected_version=9)
        self.assertEqual((state.score,state.manual_grant,state.version),(150,4,10))
        self.assertEqual(migrated.record_event(self.a,'evidence','shared_activity',1,source_user_id='100').reason,'duplicate')

    def test_migration_rejects_custom_schema_and_backup_overwrite(self):
        old = self.old_signed_database()
        backup = Path(self.tmp.name) / "backup.sqlite3"
        backup.write_text('DO NOT OVERWRITE',encoding='utf-8')
        before = old.read_bytes()
        with self.assertRaises(FileExistsError):
            core.migrate_score_range(old,backup)
        self.assertEqual(old.read_bytes(),before)
        self.assertEqual(backup.read_text(encoding='utf-8'),'DO NOT OVERWRITE')
        with closing(sqlite3.connect(old)) as db:
            db.execute("CREATE INDEX custom_relation_score ON relations(score)")
            db.commit()
        with self.assertRaisesRegex(ValueError,'custom'):
            core.migrate_score_range(old,Path(self.tmp.name)/'unused.sqlite3')
        self.assertFalse((Path(self.tmp.name)/'unused.sqlite3').exists())

    def test_invalid_configuration(self):
        for kwargs in ({"thresholds": (60, 20)}, {"thresholds": (0, 20)},
                       {"daily_cap": float("nan")}, {"max_delta": 0},
                       {"cooldown_seconds": -1}, {"daily_event_limit": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RelationStore(self.path, **kwargs)
        with self.assertRaises(ValueError):
            RelationStore(":memory:")


if __name__ == "__main__":
    unittest.main()
