"""Administrator-only relationship extra settings, using the real SDK/Pydantic v2 API.

These strings are shared per-level interaction settings, never personal secrets.
They are NOT an instruction-injection security barrier.
Only trusted administrators may configure them. The Host must independently enforce
identity, authorization, privacy, group caps and the current stop/consent rules;
no wording (including the fixed reminder below) can grant or revoke permission.
The lead plugin owns configuration loading/hot updates and reply-anchor binding.

Integration: inherit ``StyleConfig`` in the root plugin config so SDK 2.21's
one-level WebUI schema exposes eight sections, each with three editable strings.
``private_hint`` is the shared canonical setting; legacy ``group_hint`` survives
serialization for recovery, is absent from the UI schema and is never rendered.
Do not nest StyleConfig under another section: that SDK does not recursively
expand nested object fields into editable sections.
"""
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any
import unicodedata

from maibot_sdk import Field, PluginConfigBase
from pydantic import ConfigDict, ValidationError, ValidationInfo, field_validator, model_validator


LABEL_MAX_LENGTH = 48
HINT_MAX_LENGTH = 1200
AVOID_MAX_LENGTH = 600
MAX_RENDER_LENGTH = 2200

# Generic public defaults; only missing fields receive defaults.
# No file I/O or automatic replacement of an existing configuration occurs.
# Shared level settings must not contain personal secrets. Immutable at both levels;
# factories never share mutable defaults with callers.
DEFAULTS = MappingProxyType({
    'level_minus_2': MappingProxyType({'label': '反感', 'private_hint': '对当前对象保持距离，少主动亲近；结合基础人格表达不耐烦，不无端迁怒旁人。', 'avoid': '不泄隐私，不将评分当公开羞辱理由；对方认真叫停就收。', 'group_hint': '对当前对象保持距离，少主动亲近；结合基础人格表达不耐烦，不无端迁怒旁人。'}),
    'level_minus_1': MappingProxyType({'label': '疏远', 'private_hint': '相处略有保留，回应简短自然，不强装熟悉。', 'avoid': '不翻无关旧账，不把普通玩笑自动当成恶意。', 'group_hint': '相处略有保留，回应简短自然，不强装熟悉。'}),
    'level_0': MappingProxyType({'label': '普通', 'private_hint': '按基础人格自然聊天，不假装共同经历或亲密关系。', 'avoid': '不默认认亲，不编承诺和关系。', 'group_hint': '按基础人格自然聊天，不假装共同经历或亲密关系。'}),
    'level_1': MappingProxyType({'label': '熟悉', 'private_hint': '比初识更随意，可接当前延续的梗和双方认可的称呼。', 'avoid': '熟悉不等于当前亲密授权。', 'group_hint': '比初识更随意，可接当前延续的梗和双方认可的称呼。'}),
    'level_2': MappingProxyType({'label': '友人', 'private_hint': '表现朋友式信任，关心和感谢可直接表达，互损保持一起玩的兴致。', 'avoid': '不公开私聊细节，不因护短改变事实。', 'group_hint': '表现朋友式信任，关心和感谢可直接表达，互损保持一起玩的兴致。'}),
    'level_3': MappingProxyType({'label': '亲切', 'private_hint': '更愿意表达在意，温柔接住善意；结合当前意愿和基础人格相处。', 'avoid': '不把档位当成恋爱或服从承诺。', 'group_hint': '更愿意表达在意，温柔接住善意；结合当前意愿和基础人格相处。'}),
    'level_4': MappingProxyType({'label': '亲密', 'private_hint': '相处更坦然亲近，可自然表达珍惜和喜欢；仍接眼前话题，不每句都变情话。', 'avoid': '不控制现实交往，不以关系要求对方继续互动。', 'group_hint': '相处更坦然亲近，可自然表达珍惜和喜欢；仍接眼前话题，不每句都变情话。'}),
    'level_5': MappingProxyType({'label': '管理员', 'private_hint': '对配置的管理员表现最熟悉、偏心和温柔；仍按真实语境，不变成命令服从或管理口吻。', 'avoid': '相处语气不授予额外工具权限，不泄露管理员个人信息。', 'group_hint': '对配置的管理员表现最熟悉、偏心和温柔；仍按真实语境，不变成命令服从或管理口吻。'}),
})

# Fixed reminder is deliberately not a configurable field. Actual enforcement
# belongs to trusted code, not this text or any semantic keyword filter.
# Internal prompt guidance, never user-facing reply text. Caller owns enforcement.
BOUNDARY_TEXT = "仅描述相处方式，不改变身份权限；按当前对象及其意愿使用。"
HARD_BOUNDARY_REMINDER = BOUNDARY_TEXT  # Compatibility alias; rendered only once.


def _validate_text(value: Any, *, multiline: bool, preserve: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("style text must be a string")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
           and not (char == "\n" and multiline) for char in value):
        raise ValueError("style text must not contain control or format characters")
    if not value.strip():
        raise ValueError("style text must not be blank")
    return value if preserve else value.strip()


class LevelStyle(PluginConfigBase):
    """仅管理员可编辑的分级相处额外设定；多人共用，不含个人秘密，非授权或隐私安全屏障。"""

    # Only legacy group_hint may survive as extra data. It is absent from
    # model_fields, so the actual SDK WebUI schema cannot offer a second editor.
    model_config = ConfigDict(validate_assignment=True, extra="allow")

    label: str = Field(default="自然礼貌", strict=True, min_length=1, max_length=LABEL_MAX_LENGTH,
                       description="相处阶段名称（不是身份授权；修改名称不改变权限）",
                       json_schema_extra={"label": "阶段名称", "max_length": LABEL_MAX_LENGTH})
    private_hint: str = Field(default="保持自然礼貌，尊重边界。", strict=True,
                              min_length=1, max_length=HINT_MAX_LENGTH,
                              description="群聊与私聊共用的额外设定，可补充相处方式；仅管理员配置，多人共用勿填个人秘密，不改变硬授权、隐私或当前叫停规则",
                              json_schema_extra={"label": "额外设定", "max_length": HINT_MAX_LENGTH,
                                                 "x-widget": "textarea", "rows": 4})
    avoid: str = Field(default="不强迫亲近，不披露私聊细节。", strict=True,
                       min_length=1, max_length=AVOID_MAX_LENGTH,
                       description="额外设定的避免事项；多人共用勿填个人秘密，不能替代或覆盖硬授权、隐私与当前叫停规则",
                       json_schema_extra={"label": "避免事项", "max_length": AVOID_MAX_LENGTH,
                                          "x-widget": "textarea", "rows": 3})

    def __setattr__(self, name: str, value: Any) -> None:
        # Match extra='ignore' assignment semantics: unknown attributes cannot
        # become persisted extras through validate_assignment's separate path.
        if not name.startswith("_") and name not in type(self).model_fields and name != "group_hint":
            raise ValueError(f"Unknown style field: {name}")
        super().__setattr__(name, value)

    @model_validator(mode="before")
    @classmethod
    def preserve_legacy_group_only(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        # Preserve legacy text verbatim, including surrounding whitespace; never
        # merge it into canonical text or render it. Drop other unknown extras
        # exactly as the SDK base's extra='ignore' did before this migration.
        known = {key: item for key, item in value.items() if key in cls.model_fields}
        if "group_hint" in value:
            legacy = _validate_text(value["group_hint"], multiline=True, preserve=True)
            if len(legacy) > HINT_MAX_LENGTH:
                raise ValueError("legacy group_hint exceeds maximum length")
            known["group_hint"] = legacy
        return known

    @field_validator("label", "private_hint", "avoid", mode="before")
    @classmethod
    def validate_text(cls, value: Any, info: ValidationInfo) -> str:
        # Textareas accept LF only; CR/CRLF remain rejected. Label is single-line.
        return _validate_text(value, multiline=info.field_name != "label")


class LevelMinus2Style(LevelStyle):
    __ui_label__ = "−2 · 反感"
    __ui_order__ = 0


class LevelMinus1Style(LevelStyle):
    __ui_label__ = "−1 · 疏远"
    __ui_order__ = 1


class Level0Style(LevelStyle):
    __ui_label__ = "0 · 普通"
    __ui_order__ = 2


class Level1Style(LevelStyle):
    __ui_label__ = "1 · 熟悉"
    __ui_order__ = 3


class Level2Style(LevelStyle):
    __ui_label__ = "2 · 友人"
    __ui_order__ = 4


class Level3Style(LevelStyle):
    __ui_label__ = "3 · 亲切"
    __ui_order__ = 5


class Level4Style(LevelStyle):
    __ui_label__ = "4 · 亲密"
    __ui_order__ = 6


class Level5Style(LevelStyle):
    __ui_label__ = "5 · 管理员"
    __ui_order__ = 7


_LEVEL_TYPES = MappingProxyType({
    "level_minus_2": LevelMinus2Style,
    "level_minus_1": LevelMinus1Style,
    "level_0": Level0Style,
    "level_1": Level1Style,
    "level_2": Level2Style,
    "level_3": Level3Style,
    "level_4": Level4Style,
    "level_5": Level5Style,
})


def _default(field: str) -> LevelStyle:
    return _LEVEL_TYPES[field].model_validate(dict(DEFAULTS[field]))


class StyleConfig(PluginConfigBase):
    """管理员配置的八级相处额外设定；多人共用，不含个人秘密。身份、隐私和当前叫停规则由可信代码独立执行。"""

    level_minus_2: LevelMinus2Style = Field(default_factory=lambda: _default("level_minus_2"))
    level_minus_1: LevelMinus1Style = Field(default_factory=lambda: _default("level_minus_1"))
    level_0: Level0Style = Field(default_factory=lambda: _default("level_0"))
    level_1: Level1Style = Field(default_factory=lambda: _default("level_1"))
    level_2: Level2Style = Field(default_factory=lambda: _default("level_2"))
    level_3: Level3Style = Field(default_factory=lambda: _default("level_3"))
    level_4: Level4Style = Field(default_factory=lambda: _default("level_4"))
    level_5: Level5Style = Field(default_factory=lambda: _default("level_5"))

    @field_validator(*DEFAULTS, mode="before")
    @classmethod
    def fill_partial_defaults(cls, value: Any, info: ValidationInfo) -> Any:
        if isinstance(value, Mapping):
            return {**DEFAULTS[info.field_name], **value}
        return value


def restore_defaults() -> StyleConfig:
    """Return a fresh default instance, without mutating any current config."""
    return StyleConfig()


def default_styles() -> StyleConfig:
    """Integration alias: return a new independently mutable default model."""
    return restore_defaults()


def get_style(level: int, private: bool, styles: StyleConfig | Mapping[str, Any], *,
              allow_group_high_levels: bool = False) -> str:
    """Pure, bounded internal prompt renderer; never send its text as a reply.

    Invalid input or group level >2 without explicit opt-in returns empty text.

    The caller must pass an independently authorized projection level, NOT a raw
    score/user request. This function cannot authenticate level 5 or any sender.
    Revalidation also rejects model_construct/unchecked model_copy bypasses.
    """
    if type(level) is not int or level not in range(-2, 6) or type(private) is not bool:
        return ""
    if type(allow_group_high_levels) is not bool:
        return ""
    if not private and level > 2 and not allow_group_high_levels:
        return ""
    if not isinstance(styles, (StyleConfig, Mapping)):
        return ""
    data = styles.model_dump() if isinstance(styles, StyleConfig) else dict(styles)
    try:
        validated = StyleConfig.model_validate(data)
    except (ValidationError, TypeError, ValueError):
        return ""
    name = f"level_minus_{-level}" if level < 0 else f"level_{level}"
    style = getattr(validated, name)
    text = (f"[关系额外设定：{style.label}] {style.private_hint} "
            f"避免事项：{style.avoid} {BOUNDARY_TEXT}")
    return text if len(text) <= MAX_RENDER_LENGTH else ""
