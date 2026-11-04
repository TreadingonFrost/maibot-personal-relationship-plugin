"""Standalone, standard-library relationship state; no Host, network or model imports.

Adapter contract: bind ONE bot/role namespace to a separate database, authenticate
admin identity outside this module, then pass that exact Subject to ``grant``.
Never resolve nicknames into Subjects. A model's analysis is not authentication.
No API accepts conversation bodies, notes or arbitrary metadata. Only the fixed
admin's authenticated adapter may replace scores via admin_set_score; never
expose that method to model tools or accept model-claimed admin identity.
Each method uses a fresh SQLite connection; use asyncio.to_thread(method, ...) if
called on an event loop. No persistent connection or close() is necessary.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
import re
from pathlib import Path
import sqlite3
import time
from typing import Callable, Iterable, Literal


@dataclass(frozen=True)
class Subject:
    """Exact platform/account identity, never a nickname or cross-platform alias."""
    platform: str
    user_id: str

    def __post_init__(self) -> None:
        for value in (self.platform, self.user_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("subject fields must be nonempty strings")


def score_rank(score: float) -> int:
    """Display rank only; 100/150 ranks 3/4 NEVER grant automatic authority."""
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not -200 <= score <= 200:
        raise ValueError("score must be finite and between -200 and 200")
    if score < 0:
        return -sum(abs(score) >= threshold for threshold in (30, 70))
    return sum(score >= threshold for threshold in (30, 70, 100, 150))


class VersionConflict(RuntimeError):
    """Expected version is stale; no state or dedup/audit entry was changed."""


@dataclass(frozen=True)
class RelationState:
    subject: Subject
    score: float = 0.0
    auto_level: int = 0
    manual_grant: int = 0
    version: int = 0
    is_admin: bool = False

    @property
    def score_rank(self) -> int:
        return score_rank(self.score)

    @property
    def effective_level(self) -> int:
        """Identity-derived 5; otherwise manual 3/4 or signed auto -2..2."""
        if self.is_admin:
            return 5
        return self.manual_grant if self.manual_grant else self.auto_level


@dataclass(frozen=True)
class EventResult:
    accepted: bool
    reason: str
    state: RelationState


def migrate_score_range(db_path: str | Path, backup_path: str | Path) -> None:
    """Explicit offline -100..100 -> -200..200 migration with exclusive backup.

    Never called by the constructor. IMMEDIATE lock holds writers from the
    backup snapshot through migration commit. Deploy new logic promptly. Only
    the known signed relations table layout is accepted, with no custom triggers,
    indexes or referencing foreign keys; unknown schema fails before any backup.
    Every relation value/version/counter and all other tables remain unchanged.
    backup_path must not exist. SQLite online backup is verified, then table
    replacement occurs in one IMMEDIATE transaction and rolls back on failure.
    """
    source_path, destination = Path(db_path).resolve(), Path(backup_path).resolve()
    if source_path == destination or not source_path.is_file():
        raise ValueError("existing source and distinct new backup path required")
    from contextlib import closing
    with closing(sqlite3.connect(str(source_path), timeout=30)) as db:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='relations'").fetchone()
        compact = "".join(row[0].lower().split()) if row else ""
        if "score>=-100andscore<=100" not in compact:
            raise ValueError("only known signed -100..100 schema can be migrated")
        columns = db.execute("PRAGMA table_info(relations)").fetchall()
        expected = [('platform','TEXT',1,None,1),('user_id','TEXT',1,None,2),
                    ('score','REAL',1,'0',0),('manual_grant','INTEGER',1,'0',0),
                    ('version','INTEGER',1,'0',0),('last_event','REAL',0,None,0),
                    ('day','INTEGER',0,None,0),('daily_points','REAL',1,'0',0),
                    ('daily_count','INTEGER',1,'0',0)]
        if [(r[1],r[2],r[3],r[4],r[5]) for r in columns] != expected:
            raise ValueError("unknown relations columns; manual migration required")
        objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='relations' AND type IN ('index','trigger')").fetchall()
        if any(kind == 'trigger' or sql is not None for kind, _, sql in objects):
            raise ValueError("custom relations index/trigger requires manual migration")
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            escaped = table.replace('"', '""')
            if any(fk[2] == 'relations' for fk in db.execute(f'PRAGMA foreign_key_list("{escaped}")')):
                raise ValueError("referencing foreign key requires manual migration")
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='relations_range_migration'").fetchone():
            raise ValueError("migration temporary table already exists")
        # Lock writers BEFORE snapshotting, using a second read connection for
        # backup (backing up the connection's own write transaction would block).
        db.execute("BEGIN IMMEDIATE")
        with destination.open('xb'):
            pass
        with closing(sqlite3.connect(str(source_path), timeout=30)) as reader:
            with closing(sqlite3.connect(str(destination))) as backup:
                reader.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != 'ok':
                    raise ValueError("backup integrity check failed")
        replacement = row[0].replace('relations', 'relations_range_migration', 1)
        replacement = re.sub(r'score\s*>=\s*-100\s+AND\s+score\s*<=\s*100',
                             'score >= -200 AND score <= 200', replacement, flags=re.IGNORECASE)
        with db:
            db.execute(replacement)
            db.execute("INSERT INTO relations_range_migration SELECT * FROM relations")
            db.execute("DROP TABLE relations")
            db.execute("ALTER TABLE relations_range_migration RENAME TO relations")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != 'ok':
                raise ValueError("migrated database integrity check failed")


class RelationStore:
    """Independent SQLite store with serialized writes and optimistic versions.

    ``daily_cap`` bounds ABSOLUTE score movement per UTC day and subject:
    positive and negative evidence share a nonrefundable budget. max_delta
    bounds each change; daily_event_limit bounds successful evidence events.
    This is a TRUSTED evidence API, not a model-callable scoring tool. Adapters
    must authenticate source and validate explicit evidence, never infer a
    violation from ordinary teasing or a single swear word. No online model is
    called here. message events (of either sign) never change score.
    All valid inbound IDs, including cooldown/cap rejections, are consumed once
    per subject. clock is trusted infrastructure, not inbound client time.
    Ordinary negative evidence is damped using the pre-event effective level
    (-2..5 indexes negative_relation_factors), multiplied by a UTC-day factor:
    max(negative_min_factor, negative_daily_decay ** max(0, n - threshold)).
    n counts accepted negative changes INCLUDING the current event; positive
    events never reset it. With defaults n=1..3 use 1, n=4 uses .5, n=5 .25.
    The floor applies to the DAILY factor; relationship and daily factors then
    multiply raw magnitude BEFORE max_delta, absolute budget and score clipping.
    targeted_harassment bypasses BOTH factors (never a high-trust exemption),
    but still uses all limits and increments accepted negative count. Set all
    relationship factors and negative_daily_decay to 1 to disable damping.
    Counts are private storage, never added to projections or EventResult.
    This schema requires signed scores; old unsigned schemas fail closed
    without migration or writes. Only offline/new database paths should be used.
    """
    EVENT_KINDS = frozenset({"meaningful_support", "shared_activity", "mutual_humor"})
    NEGATIVE_EVENT_KINDS = frozenset({"repeated_boundary_violation", "targeted_harassment"})

    def __init__(self, db_path: str | Path, *,
                 admin_subjects: Iterable[Subject] = (),
                 thresholds: tuple[float, float] = (30.0, 70.0),
                 cooldown_seconds: float = 60.0, daily_cap: float = 10.0,
                 max_delta: float = 5.0, daily_event_limit: int = 20,
                 negative_relation_factors: tuple[float, ...] = (1, 1, 1, .9, .8, .7, .6, .6),
                 negative_daily_threshold: int = 3,
                 negative_daily_decay: float = .5,
                 negative_min_factor: float = .1,
                 clock: Callable[[], float] = time.time) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("short connections require a dedicated file database")
        self.db_path = str(db_path)
        configured_admins = tuple(admin_subjects)
        if len(configured_admins) > 1 or any(
                not isinstance(subject, Subject) or subject.platform != "qq"
                or re.fullmatch(r"[1-9][0-9]{0,19}", subject.user_id) is None
                for subject in configured_admins):
            raise ValueError("configure at most one exact numeric QQ administrator")
        # No built-in identity. An empty configuration disables admin operations.
        self._admin_subject = configured_admins[0] if configured_admins else None
        if len(thresholds) != 2:
            raise ValueError("two thresholds required")
        low, high = (self._number(x) for x in thresholds)
        if not 0 < low < high <= 200:
            raise ValueError("thresholds must satisfy 0 < low < high <= 200")
        self.thresholds = (low, high)
        self.cooldown_seconds = self._number(cooldown_seconds)
        self.daily_cap = self._number(daily_cap)
        self.max_delta = self._number(max_delta)
        if self.cooldown_seconds < 0 or self.daily_cap <= 0 or self.max_delta <= 0:
            raise ValueError("invalid limits")
        if type(daily_event_limit) is not int or daily_event_limit <= 0:
            raise ValueError("daily_event_limit must be positive integer")
        self.daily_event_limit = daily_event_limit
        if not isinstance(negative_relation_factors, tuple) or len(negative_relation_factors) != 8:
            raise ValueError("negative_relation_factors must be a tuple for levels -2..5")
        self.negative_relation_factors = tuple(self._factor(x) for x in negative_relation_factors)
        if type(negative_daily_threshold) is not int or negative_daily_threshold <= 0:
            raise ValueError("negative_daily_threshold must be a positive integer")
        self.negative_daily_threshold = negative_daily_threshold
        self.negative_daily_decay = self._factor(negative_daily_decay)
        self.negative_min_factor = self._factor(negative_min_factor)
        self.clock = clock
        with self._connect() as db:
            existing = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='relations'").fetchone()
            if existing and "score>=-200andscore<=200" not in "".join(existing[0].lower().split()):
                raise ValueError("legacy score range requires explicit offline migration")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS relations (
                    platform TEXT NOT NULL, user_id TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0 CHECK(score >= -200 AND score <= 200),
                    manual_grant INTEGER NOT NULL DEFAULT 0 CHECK(manual_grant IN (0,3,4)),
                    version INTEGER NOT NULL DEFAULT 0,
                    last_event REAL, day INTEGER, daily_points REAL NOT NULL DEFAULT 0,
                    daily_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(platform,user_id));
                CREATE TABLE IF NOT EXISTS inbound (
                    platform TEXT NOT NULL, user_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, kind TEXT NOT NULL,
                    source_user_id TEXT NOT NULL, awarded REAL NOT NULL,
                    created REAL NOT NULL,
                    PRIMARY KEY(platform,user_id,message_id));
                CREATE TABLE IF NOT EXISTS negative_adjustments (
                    platform TEXT NOT NULL, user_id TEXT NOT NULL, day INTEGER NOT NULL,
                    count INTEGER NOT NULL CHECK(count >= 0),
                    PRIMARY KEY(platform,user_id,day));
                CREATE TABLE IF NOT EXISTS score_audit (
                    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, user_id TEXT NOT NULL,
                    admin_platform TEXT NOT NULL, admin_user_id TEXT NOT NULL,
                    old_score REAL NOT NULL, new_score REAL NOT NULL,
                    version INTEGER NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS grant_audit (
                    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, user_id TEXT NOT NULL,
                    admin_platform TEXT NOT NULL, admin_user_id TEXT NOT NULL,
                    old_level INTEGER NOT NULL, new_level INTEGER NOT NULL,
                    version INTEGER NOT NULL, created REAL NOT NULL);
            ''')

    @property
    def admin_subject(self) -> Subject | None:
        """Trusted constructor configuration; never learned from a message/model."""
        return self._admin_subject

    @property
    def admin_subjects(self) -> frozenset[Subject]:
        """Compatibility view, immutable and empty when admin is unconfigured."""
        return frozenset({self._admin_subject}) if self._admin_subject is not None else frozenset()

    def is_admin(self, subject: Subject) -> bool:
        return self._admin_subject is not None and subject == self._admin_subject

    @staticmethod
    def _number(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("finite numeric value required")
        try:
            number = float(value)
        except (ValueError, OverflowError):
            raise ValueError("finite numeric value required") from None
        if not math.isfinite(number):
            raise ValueError("finite numeric value required")
        return number

    @classmethod
    def _factor(cls, value: float) -> float:
        factor = cls._number(value)
        if not 0 < factor <= 1:
            raise ValueError("negative factors must satisfy 0 < factor <= 1")
        return factor

    @staticmethod
    def _subject(subject: Subject) -> None:
        if not isinstance(subject, Subject):
            raise ValueError("exact Subject required; nicknames are unsupported")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _state(self, subject: Subject, row: sqlite3.Row | None) -> RelationState:
        if self.is_admin(subject):
            return RelationState(subject, 200.0, 2, row["manual_grant"] if row else 0,
                                 row["version"] if row else 0, is_admin=True)
        if row is None:
            return RelationState(subject)
        score = float(row["score"])
        level = (1 if score >= 0 else -1) * sum(abs(score) >= threshold for threshold in self.thresholds)
        return RelationState(subject, score, level, row["manual_grant"], row["version"])

    @staticmethod
    def _row(db: sqlite3.Connection, subject: Subject) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM relations WHERE platform=? AND user_id=?",
                          (subject.platform, subject.user_id)).fetchone()

    @staticmethod
    def _version(state: RelationState, expected: int | None) -> None:
        if expected is not None:
            if type(expected) is not int or expected < 0:
                raise ValueError("expected_version must be a nonnegative integer")
            if expected != state.version:
                raise VersionConflict("relationship version changed")

    def _admin(self, authenticated_admin: Subject) -> None:
        self._subject(authenticated_admin)
        if not self.is_admin(authenticated_admin):
            raise PermissionError("administrator authentication required")

    def record_event(self, subject: Subject, message_id: str, kind: str,
                     delta: float, *, source_user_id: str,
                     expected_version: int | None = None) -> EventResult:
        """Consume one evidence ID atomically; never accept message text.

        Trusted adapters only: source_user_id must exactly match the target.
        Positive kinds require delta >= 0; repeated_boundary_violation and
        targeted_harassment require delta < 0 and independently verified evidence.
        Ordinary teasing or a single profanity is NOT evidence of either kind.
        Changes are capped by max_delta, absolute UTC budget and score room.
        message events with either sign earn zero. Unknown kinds are rejected.
        """
        self._subject(subject)
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("nonempty message ID required")
        if not isinstance(source_user_id, str) or source_user_id != subject.user_id:
            raise ValueError("evidence source must equal target user_id")
        if not isinstance(kind, str) or kind not in self.EVENT_KINDS | self.NEGATIVE_EVENT_KINDS | {"message"}:
            raise ValueError("unsupported event kind")
        amount = self._number(delta)
        if (kind in self.EVENT_KINDS and amount < 0) or (kind in self.NEGATIVE_EVENT_KINDS and amount >= 0):
            raise ValueError("delta sign must match the explicit evidence kind")
        now = self._number(self.clock())
        day = math.floor(now / 86400)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, subject)
            state = self._state(subject, row)
            self._version(state, expected_version)
            if self.is_admin(subject):
                return EventResult(False, "fixed_admin_score", state)
            key = (subject.platform, subject.user_id)
            if db.execute("SELECT 1 FROM inbound WHERE platform=? AND user_id=? AND message_id=?",
                          (*key, message_id)).fetchone():
                return EventResult(False, "duplicate", state)
            daily_points = row["daily_points"] if row and row["day"] == day else 0
            daily_count = row["daily_count"] if row and row["day"] == day else 0
            reason = "accepted"
            if kind == "message":
                reason = "not_meaningful"
            elif amount == 0:
                reason = "zero_delta"
            elif row and row["last_event"] is not None and now - row["last_event"] < self.cooldown_seconds:
                reason = "cooldown"
            elif daily_points >= self.daily_cap or daily_count >= self.daily_event_limit:
                reason = "daily_limit"
            elif (amount > 0 and state.score >= 200) or (amount < 0 and state.score <= -200):
                reason = "score_cap"
            adjusted_amount = abs(amount)
            if reason == "accepted" and kind == "repeated_boundary_violation":
                negative_row = db.execute(
                    "SELECT count FROM negative_adjustments WHERE platform=? AND user_id=? AND day=?",
                    (*key, day)).fetchone()
                ordinal = (negative_row["count"] if negative_row else 0) + 1
                exponent = max(0, ordinal - self.negative_daily_threshold)
                daily_factor = max(self.negative_min_factor, self.negative_daily_decay ** exponent)
                relation_factor = self.negative_relation_factors[state.effective_level + 2]
                adjusted_amount *= relation_factor * daily_factor
            direction = 1 if amount >= 0 else -1
            room = 200 - state.score if direction > 0 else state.score + 200
            award = direction * min(adjusted_amount, self.max_delta, self.daily_cap - daily_points, room) if reason == "accepted" else 0.0
            # Floating-point subnormals or exhausted score precision cannot create
            # accepted zero-movement evidence or consume negative ordinals.
            if reason == "accepted" and state.score + award == state.score:
                award, reason = 0.0, "zero_delta"
            db.execute("INSERT INTO inbound VALUES (?,?,?,?,?,?,?)",
                       (*key, message_id, kind, source_user_id, award, now))
            if award != 0:
                if award < 0:
                    db.execute("INSERT INTO negative_adjustments(platform,user_id,day,count) VALUES (?,?,?,1) ON CONFLICT(platform,user_id,day) DO UPDATE SET count=count+1", (*key, day))
                db.execute("INSERT OR IGNORE INTO relations(platform,user_id) VALUES (?,?)", key)
                db.execute("UPDATE relations SET score=score+?, version=version+1, last_event=?, day=?, daily_points=?, daily_count=? WHERE platform=? AND user_id=?",
                           (award, now, day, daily_points + abs(award), daily_count + 1, *key))
                state = self._state(subject, self._row(db, subject))
            return EventResult(award != 0, reason, state)

    def grant(self, subject: Subject, level: int, *, authenticated_admin: Subject,
              expected_version: int | None = None) -> RelationState:
        """Set/revoke independent manual privilege, atomically recording audit.

        authenticated_admin MUST come from adapter-authenticated event sender,
        not a model tool argument, message body, display name or claimed UID.
        No score changes occur. 0 revokes; only 3 and 4 grant privileges.
        """
        self._admin(authenticated_admin)
        self._subject(subject)
        if type(level) is not int or level not in (0, 3, 4):
            raise ValueError("manual level must be 0, 3 or 4")
        now = self._number(self.clock())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(subject, self._row(db, subject))
            self._version(state, expected_version)
            key = (subject.platform, subject.user_id)
            db.execute("INSERT OR IGNORE INTO relations(platform,user_id) VALUES (?,?)", key)
            db.execute("UPDATE relations SET manual_grant=?,version=version+1 WHERE platform=? AND user_id=?", (level, *key))
            db.execute("INSERT INTO grant_audit(platform,user_id,admin_platform,admin_user_id,old_level,new_level,version,created) VALUES (?,?,?,?,?,?,?,?)",
                       (*key, authenticated_admin.platform, authenticated_admin.user_id,
                        state.manual_grant, level, state.version + 1, now))
            return self._state(subject, self._row(db, subject))

    def admin_set_score(self, subject: Subject, score: float, *,
                        authenticated_admin: Subject,
                        expected_version: int | None = None) -> RelationState:
        """Admin-only exact score replacement, never a model-scoring interface.

        Require the adapter-authenticated configured administrator, exact target Subject, and a
        finite score in [-200,200]; out-of-range values are rejected, not clipped.
        Atomic CAS, version increment and old/new/executor audit apply even when
        setting the same score. Manual grants and identity-derived admin level 5
        never change. This bypasses automatic daily budget WITHOUT consuming or
        resetting budget, cooldown, dedup or negative damping counters.
        For adjustment, admin_inspect then calculate and set with expected_version;
        a stale version must be re-read, never retried as an unchecked overwrite.
        """
        self._admin(authenticated_admin)
        self._subject(subject)
        if self.is_admin(subject):
            raise ValueError("global admin score is fixed at 200")
        value = self._number(score)
        if not -200 <= value <= 200:
            raise ValueError("score must be between -200 and 200")
        now = self._number(self.clock())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(subject, self._row(db, subject))
            self._version(state, expected_version)
            key = (subject.platform, subject.user_id)
            db.execute("INSERT OR IGNORE INTO relations(platform,user_id) VALUES (?,?)", key)
            db.execute("UPDATE relations SET score=?,version=version+1 WHERE platform=? AND user_id=?", (value, *key))
            db.execute("INSERT INTO score_audit(platform,user_id,admin_platform,admin_user_id,old_score,new_score,version,created) VALUES (?,?,?,?,?,?,?,?)",
                       (*key, authenticated_admin.platform, authenticated_admin.user_id,
                        state.score, value, state.version + 1, now))
            return self._state(subject, self._row(db, subject))

    def projection(self, subject: Subject,
                   audience: Literal["self_private", "public_group"],
                   viewer: Subject, *, allow_group_high_levels: bool = False) -> dict:
        """Return a fresh allowlisted dictionary, never evidence/audit/note text.

        Public keys remain subject, familiarity, trust_summary, effective_level.
        By default public effective_level is capped at 2 (admin familiarity 2,
        all others use auto_level). Trusted adapter configuration may explicitly
        enable allow_group_high_levels to expose the EXISTING effective level,
        including manual 3/4 and identity-derived 5. It never creates authority.
        Summaries remain generic familiarity, with no grant/admin/private facts.
        Private adds score, auto_level, manual_grant and version only when
        viewer == subject; the group switch never bypasses this identity check.
        Admin inspection remains an independent explicitly targeted method.
        """
        self._subject(subject)
        self._subject(viewer)
        if type(allow_group_high_levels) is not bool:
            raise ValueError("allow_group_high_levels must be a boolean")
        if audience not in ("self_private", "public_group"):
            raise ValueError("unsupported audience")
        if audience == "self_private" and viewer != subject:
            raise PermissionError("private projection belongs only to its subject")
        with self._connect() as db:
            state = self._state(subject, self._row(db, subject))
        # Public admin familiarity is fixed policy, not an authorization field.
        public_level = 2 if self.is_admin(subject) else state.auto_level
        if audience == "public_group" and allow_group_high_levels:
            public_level = state.effective_level
        summaries = {
            -2: ("boundaries_required", "repeated_boundary_concerns"),
            -1: ("cautious", "boundary_concerns"),
            0: ("new", "still_getting_acquainted"),
            1: ("familiar", "positive_shared_experience"),
            2: ("close", "established_shared_trust"),
            3: ("close", "established_shared_trust"),
            4: ("close", "established_shared_trust"),
            5: ("close", "established_shared_trust"),
        }
        familiarity, trust_summary = summaries[public_level]
        result = {
            "subject": {"platform": subject.platform, "user_id": subject.user_id},
            "familiarity": familiarity,
            "trust_summary": trust_summary,
            "effective_level": public_level,
        }
        if audience == "self_private":
            result.update(score=state.score, auto_level=state.auto_level,
                          manual_grant=state.manual_grant,
                          effective_level=state.effective_level, version=state.version)
        return result

    def admin_inspect(self, subject: Subject, *, authenticated_admin: Subject) -> dict:
        """Admin-only explicit target inspection; no search/list-all API.

        Returns target's state and only target's grant/score audits. Evidence
        IDs, inbound source records and other people's state are not returned.
        """
        self._admin(authenticated_admin)
        self._subject(subject)
        with self._connect() as db:
            db.execute("BEGIN")
            state = self._state(subject, self._row(db, subject))
            audits = db.execute("SELECT admin_platform,admin_user_id,old_level,new_level,version,created FROM grant_audit WHERE platform=? AND user_id=? ORDER BY id",
                                (subject.platform, subject.user_id)).fetchall()
            score_audits = db.execute("SELECT admin_platform,admin_user_id,old_score,new_score,version,created FROM score_audit WHERE platform=? AND user_id=? ORDER BY id",
                                      (subject.platform, subject.user_id)).fetchall()
        return {"subject": {"platform": subject.platform, "user_id": subject.user_id},
                "score": state.score, "auto_level": state.auto_level,
                "manual_grant": state.manual_grant, "effective_level": state.effective_level,
                "version": state.version, "grant_audit": [dict(row) for row in audits],
                "score_audit": [dict(row) for row in score_audits]}
