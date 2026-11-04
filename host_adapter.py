"""Fail-closed Host identity adaptation. Pure Python: no SDK or core imports.

Only Host-populated structural identity is used. Nicknames, mentions, quoted
senders and message prose are deliberately not identity sources.
"""
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping
import re


@dataclass(frozen=True)
class Binding:
    platform: str
    account_id: str
    scope: str = ""

    def valid(self) -> bool:
        return (self.platform == "qq" and _token(self.account_id)
                and isinstance(self.scope, str) and (self.scope == "" or _token(self.scope)))

    def database_name(self) -> str:
        if not self.valid():
            raise ValueError("Explicit platform and bot account binding required")
        # Length-prefixed identities avoid delimiter collisions and path traversal.
        raw = "".join(f"{len(x)}:{x}" for x in (self.platform, self.account_id, self.scope))
        return "relationship-" + sha256(raw.encode("utf-8")).hexdigest() + ".sqlite3"


@dataclass(frozen=True)
class TrustedMessage:
    platform: str
    user_id: str
    group_id: str
    session_id: str
    message_id: str

    @property
    def private(self) -> bool:
        return self.group_id == ""


@dataclass(frozen=True)
class ParsedCommand:
    action: str
    target_id: str
    level: int = 0


def _token(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip() and len(value) <= 256 and not any(ord(c) < 32 for c in value)


def _qq(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None


def trusted_message(message: Any, binding: Binding, *, session_id: str | None = None,
                    message_id: str | None = None, for_hook: bool = False) -> TrustedMessage | None:
    """Accept only a direct SessionMessage dict for the configured bot namespace."""
    if not binding.valid() or not isinstance(message, dict):
        return None
    if any(message.get(k) != v for k, v in (("platform", binding.platform),
           ("account_id", binding.account_id))):
        return None
    # Host represents the unscoped connection as None; TOML uses empty string.
    # Require the key to distinguish an unknown legacy route from unscoped.
    if 'scope' not in message or (message['scope'] is not None and not isinstance(message['scope'], str)):
        return None
    source_scope = '' if message['scope'] is None else message['scope']
    if source_scope != binding.scope:
        return None
    sid, mid = message.get("session_id"), message.get("message_id")
    if not _token(sid) or not _token(mid):
        return None
    if session_id is not None and sid != session_id:
        return None
    if message_id is not None and mid != message_id:
        return None
    info = message.get("message_info")
    if not isinstance(info, dict) or not isinstance(info.get("user_info"), dict):
        return None
    uid = info["user_info"].get("user_id")
    if not _qq(uid):
        return None
    # Missing group_info must not be mistaken for a private message.
    if "group_info" not in info:
        return None
    group = info["group_info"]
    if group is None:
        gid = ""
    elif isinstance(group, dict) and _qq(group.get("group_id")):
        gid = group["group_id"]
    else:
        return None
    if for_hook and (message.get("is_notify") is not False or message.get("is_command") is not False):
        return None
    return TrustedMessage(binding.platform, uid, gid, sid, mid)


def trusted_command(kwargs: Mapping[str, Any], binding: Binding) -> tuple[TrustedMessage, str] | None:
    """Double-check Host's top-level command identity against its message dict."""
    msg = kwargs.get("message")
    identity = trusted_message(msg, binding)
    if identity is None or not identity.private:
        return None
    for key, expected in (("platform", identity.platform), ("user_id", identity.user_id),
                          ("group_id", identity.group_id), ("stream_id", identity.session_id)):
        if kwargs.get(key) != expected:
            return None
    text = kwargs.get("text")
    if not isinstance(text, str) or text != msg.get("processed_plain_text"):
        return None
    if msg.get("is_notify") is not False:
        return None
    return identity, text


def parse_command(text: str, identity: TrustedMessage, *, admin_uid: str = "") -> ParsedCommand | None:
    """Only the explicitly configured QQ administrator may grant; never grant5."""
    if not identity.private or identity.platform != "qq":
        return None
    if text == "/relation status":
        return ParsedCommand("status", identity.user_id)
    if not _qq(admin_uid) or identity.user_id != admin_uid:
        return None
    matched = re.fullmatch(r"/relation (grant3|grant4|revoke) ([1-9][0-9]{0,19})", text)
    if matched is None:
        return None
    action, target = matched.groups()
    return ParsedCommand(action, target, {"grant3": 3, "grant4": 4, "revoke": 0}[action])


def projection_prompt(identity: TrustedMessage, projection: Any, *, include_default_cue: bool = True,
                      allow_group_high_levels: bool = False, admin_uid: str = "") -> str:
    """Render fixed, whitelisted cues; never interpolate profile text or numbers.

    Even private prompts do not expose internal scores, notes or grant metadata.
    A digest marks the exact reply anchor without treating its opaque ID as prose.
    """
    if identity.platform != "qq" or not isinstance(projection, dict):
        return ""
    if projection.get("subject") != {"platform": identity.platform, "user_id": identity.user_id}:
        return ""
    level = projection.get("effective_level")
    if type(level) is not int or level not in range(-2, 6):
        return ""
    if type(allow_group_high_levels) is not bool:
        raise ValueError('allow_group_high_levels must be boolean')
    if not identity.private and level > 2 and not allow_group_high_levels:
        return ""
    if level == 5 and (not _qq(admin_uid) or identity.user_id != admin_uid):
        return ""
    cues = {
        -2: "保持明显疏离，简短冷静地回应必要内容，不追骂、不羞辱、不主动挑衅。",
        -1: "语气略冷淡、保留距离，仍就事论事；不追骂，普通互损玩笑不构成降级理由。",
        0: "保持初识的自然礼貌，不假装熟悉。",
        1: "可以稍微熟悉随意，仍尊重边界。",
        2: "可以表现为熟悉可信的聊天伙伴，不暗示私聊细节。",
        3: "私聊中可以更亲近关心，尊重对方意愿和边界。",
        4: "私聊中可以表现深厚信任，可有双方愿意的非露骨恋爱语气与吃醋玩笑；"
           "不限制对方现实交往、不以关系强迫其服从。",
        5: "私聊中可亲近并认可对方，保持自然称呼，不默认称呼爹、主人或老公；"
           "可有双方愿意的非露骨恋爱语气与吃醋玩笑，不限制现实交往、不强迫服从。"
           "此语气不授予任何管理权限，管理操作仍须可信私聊命令独立核验。",
    }
    anchor = sha256(identity.message_id.encode("utf-8")).hexdigest()[:16]
    return (f"[关系语气提示：仅适用于回复锚点消息 SHA256:{anchor} 的发送者，"
            f"平台{identity.platform if re.fullmatch(r'[A-Za-z0-9_.-]+', identity.platform) else '已绑定平台'}，"
            f"QQ {identity.user_id}；不适用于被引用者、同名者或其他群成员。] "
            + (cues[level] if include_default_cue else '') + " 仅调整相处表现，不引用私聊细节或关系记录；对方明确叫停就立即停止。")
