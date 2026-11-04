"""Host-authenticated WebUI operator facade, not a QQ chat administrator.

Trust boundary: construct with operator_enabled=True ONLY inside private plugin
@API handlers exposed by Host's authenticated declarative WebUI gateway. Do not
register these methods as tools, commands, public APIs or chat event handlers.
The switch is trusted process configuration, NOT caller-supplied authentication.
Host currently forwards no per-human principal, so audit truthfully identifies
'host_webui_operator' without inventing a QQ identity or individual username.
A host operator controls the bot's local database; that does not make their chat
QQ account the configured chat administrator. Chat core authentication rules are untouched.

Plugin wiring (inside MaiBotPlugin, import API from maibot_sdk):
    @API('relation_webui_query', public=False, version='1')
    async def relation_webui_query(self, target_qq: str = ''):
        return await asyncio.to_thread(self._relation_webui.query, target_qq)
    @API('relation_webui_set_score', public=False, version='1')
    async def relation_webui_set_score(self, target_qq: str, score: float,
                                      expected_version: int):
        return await asyncio.to_thread(self._relation_webui.set_score,
                                       target_qq, score, expected_version)
    @API('relation_webui_grant', public=False, version='1')
    async def relation_webui_grant(self, target_qq: str, level: int,
                                  expected_version: int):
        return await asyncio.to_thread(self._relation_webui.grant,
                                       target_qq, level, expected_version)

No Host imports, network, free-text notes or real database paths in this module.
All writes use the core's short-connection transaction and optimistic version.
"""
from __future__ import annotations

import re

from .relation_core import RelationState, RelationStore, Subject


class AdminWebUIBackend:
    """Explicit-target operator methods; disabled unless trusted wiring enables it.

    query('') returns an empty state, never enumerates subjects. Writes require
    an explicit ASCII numeric QQ and nonnegative expected_version. Stale queries
    fail with the core VersionConflict; never silently retry an overwrite.
    Returned allowlist omits grant/score audit, evidence and executor identities.
    """
    def __init__(self, store: RelationStore, *, operator_enabled: bool = False):
        if type(operator_enabled) is not bool:
            raise ValueError("operator_enabled must be a trusted boolean")
        self.store = store
        self._operator_enabled = operator_enabled
        if operator_enabled:
            with store._connect() as db:
                db.execute('''CREATE TABLE IF NOT EXISTS operator_audit (
                    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, user_id TEXT NOT NULL,
                    executor_kind TEXT NOT NULL CHECK(executor_kind='host_webui_operator'),
                    operation TEXT NOT NULL CHECK(operation IN ('set_score','grant')),
                    old_score REAL NOT NULL, new_score REAL NOT NULL,
                    old_grant INTEGER NOT NULL, new_grant INTEGER NOT NULL,
                    version INTEGER NOT NULL, created REAL NOT NULL)''')

    def _authorized(self) -> None:
        if not self._operator_enabled or self.store.admin_subject is None:
            raise PermissionError("trusted Host WebUI operator capability or configured administrator is missing")

    @staticmethod
    def _target(target_qq: str) -> Subject:
        if not isinstance(target_qq, str) or re.fullmatch(r"[1-9][0-9]{4,19}", target_qq) is None:
            raise ValueError("enter an exact 5..20 digit QQ account, never a nickname")
        return Subject("qq", target_qq)

    @staticmethod
    def _result(state: RelationState, message: str = "已查询指定对象") -> dict:
        return {"target_qq": state.subject.user_id, "score": state.score,
                "score_rank": state.score_rank, "auto_level": state.auto_level, "effective_level": state.effective_level,
                "manual_grant": state.manual_grant, "version": state.version,
                "message": message, "grant3": 3, "grant4": 4, "revoke": 0}

    def query(self, target_qq: str = "") -> dict:
        """Read just one exact QQ; empty default performs zero database queries."""
        self._authorized()
        if target_qq == "":
            return {"target_qq": "", "score": None, "score_rank": None, "auto_level": None,
                    "effective_level": None, "manual_grant": None, "version": None,
                    "message": "请先输入数字 QQ 并刷新；不会列出其他对象",
                    "grant3": 3, "grant4": 4, "revoke": 0}
        subject = self._target(target_qq)
        with self.store._connect() as db:
            state = self.store._state(subject, self.store._row(db, subject))
        return self._result(state)

    def set_score(self, target_qq: str, score: float, expected_version: int) -> dict:
        """Set ordinary score [-200,200]; fixed administrator score is immutable.

        Score ranks at 100/150 are display only; levels 3/4 need manual grant.
        """
        self._authorized()
        subject = self._target(target_qq)
        if self.store.is_admin(subject):
            raise ValueError("administrator score is fixed at 200 and cannot be set")
        value = self.store._number(score)
        if not -200 <= value <= 200:
            raise ValueError("score must be between -200 and 200")
        return self._write(subject, "set_score", value, expected_version)

    def grant(self, target_qq: str, level: int, expected_version: int) -> dict:
        """Host operator sets manual 0/3/4; cannot create or revoke identity 5."""
        self._authorized()
        subject = self._target(target_qq)
        if type(level) is not int or level not in (0, 3, 4):
            raise ValueError("manual grant must be 0, 3 or 4")
        return self._write(subject, "grant", level, expected_version)

    def _write(self, subject: Subject, operation: str, value: float,
               expected_version: int) -> dict:
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("query the target first; nonnegative expected_version is required")
        now = self.store._number(self.store.clock())
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            before = self.store._state(subject, self.store._row(db, subject))
            self.store._version(before, expected_version)
            key = (subject.platform, subject.user_id)
            db.execute("INSERT OR IGNORE INTO relations(platform,user_id) VALUES (?,?)", key)
            if operation == "set_score":
                db.execute("UPDATE relations SET score=?,version=version+1 WHERE platform=? AND user_id=?", (value, *key))
            else:
                db.execute("UPDATE relations SET manual_grant=?,version=version+1 WHERE platform=? AND user_id=?", (value, *key))
            after = self.store._state(subject, self.store._row(db, subject))
            db.execute("INSERT INTO operator_audit(platform,user_id,executor_kind,operation,old_score,new_score,old_grant,new_grant,version,created) VALUES (?,?,'host_webui_operator',?,?,?,?,?,?,?)",
                       (*key, operation, before.score, after.score, before.manual_grant,
                        after.manual_grant, after.version, now))
            return self._result(after, "已更新；操作以本机 WebUI operator 身份独立审计")
