"""Real SDK tests; run with system Python and MaiBot venv site-packages.

PYTHONDONTWRITEBYTECODE=1 prevents writes outside the two assigned files.
No SDK stubs, running Host, network or production databases are used.
"""
from pathlib import Path
import importlib.util
import sys
import unittest

from maibot_sdk import PluginConfigBase
from maibot_sdk.config import build_plugin_default_config, generate_plugin_config_schema
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("relationship_style_under_test", ROOT / "style_config.py")
style = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = style
spec.loader.exec_module(style)


class StyleConfigTests(unittest.TestCase):
    def test_real_sdk_base(self):
        self.assertTrue(issubclass(style.StyleConfig, PluginConfigBase))
        self.assertTrue(issubclass(style.LevelStyle, PluginConfigBase))

    def test_sdk_schema_eight_editable_sections(self):
        schema = generate_plugin_config_schema(style.StyleConfig)
        self.assertEqual(set(schema["sections"]), set(style.DEFAULTS))
        for name, section in schema["sections"].items():
            self.assertEqual(set(section["fields"]), {"label", "private_hint", "avoid"})
            for field, item in section["fields"].items():
                self.assertEqual(item["type"], "string")
                self.assertFalse(item["disabled"])
                self.assertFalse(item["hidden"])
                self.assertFalse(item["required"])
                self.assertEqual(item["default"], style.DEFAULTS[name][field])
                self.assertGreater(item["max_length"], 0)

    def test_schema_unique_ordered_section_titles(self):
        sections = generate_plugin_config_schema(style.StyleConfig)["sections"]
        titles = ["−2 · 反感", "−1 · 疏远", "0 · 普通", "1 · 熟悉",
                  "2 · 友人", "3 · 亲切", "4 · 亲密", "5 · 管理员"]
        for index, (name, title) in enumerate(zip(style.DEFAULTS, titles)):
            self.assertEqual(sections[name]["title"], title)
            self.assertEqual(sections[name]["order"], index)

    def test_schema_textarea_rows(self):
        sections = generate_plugin_config_schema(style.StyleConfig)["sections"]
        for section in sections.values():
            fields = section["fields"]
            self.assertNotEqual(fields["label"]["ui_type"], "textarea")
            for field, rows in (("private_hint", 4), ("avoid", 3)):
                self.assertEqual(fields[field]["ui_type"], "textarea")
                self.assertEqual(fields[field]["rows"], rows)

    def test_default_factory_uses_section_subclasses(self):
        cfg = style.default_styles()
        for name, field in style.StyleConfig.model_fields.items():
            self.assertIsNot(field.annotation, style.LevelStyle)
            self.assertTrue(issubclass(field.annotation, style.LevelStyle))
            self.assertIsInstance(getattr(cfg, name), field.annotation)
            self.assertIsInstance(style._default(name), field.annotation)
            self.assertEqual(style._default(name).model_dump(), dict(style.DEFAULTS[name]))

    def test_multiline_hints_and_avoid_roundtrip_render(self):
        cfg = style.default_styles()
        for field in ("private_hint", "group_hint", "avoid"):
            setattr(cfg.level_2, field, "第一行\n第二行")
        restored = style.StyleConfig.model_validate_json(cfg.model_dump_json())
        for private in (True, False):
            text = style.get_style(2, private, restored)
            self.assertEqual(text.count("第一行\n第二行"), 2)
            self.assertLessEqual(len(text), style.MAX_RENDER_LENGTH)

    def test_label_rejects_newline_and_blank_multiline_rejected(self):
        for text in ("第一行\n第二行", "标签\n", "\n标签"):
            with self.assertRaises(ValidationError):
                style.LevelStyle(label=text)
        for field in ("private_hint", "group_hint", "avoid"):
            with self.assertRaises(ValidationError):
                style.LevelStyle.model_validate({field: " \n\n "})

    def test_multiline_length_caps_include_newlines(self):
        for field, maximum in (("private_hint", style.HINT_MAX_LENGTH),
                               ("group_hint", style.HINT_MAX_LENGTH),
                               ("avoid", style.AVOID_MAX_LENGTH)):
            maximum_text = "字" + "\n" * (maximum - 2) + "字"
            self.assertEqual(style.LevelStyle.model_validate({field: maximum_text}).model_dump()[field],
                             maximum_text)
            with self.assertRaises(ValidationError):
                style.LevelStyle.model_validate({field: maximum_text + "字"})

    def test_schema_root_inheritance(self):
        class RootConfig(style.StyleConfig):
            enabled: bool = False
        schema = generate_plugin_config_schema(RootConfig)
        self.assertTrue(set(style.DEFAULTS).issubset(schema["sections"]))
        self.assertEqual(schema["sections"]["level_5"]["fields"]["label"]["default"], "管理员")

    def test_no_missing_defaults_sdk(self):
        self.assertEqual(build_plugin_default_config(style.StyleConfig),
                         {key: dict(value) for key, value in style.DEFAULTS.items()})
        self.assertFalse(style.StyleConfig.model_json_schema().get("required"))

    def test_partial_level_preserves_level_specific_defaults(self):
        cfg = style.StyleConfig.model_validate({"level_4": {"label": "自定义"}})
        self.assertEqual(cfg.level_4.label, "自定义")
        self.assertEqual(cfg.level_4.private_hint, style.DEFAULTS["level_4"]["private_hint"])
        self.assertEqual(cfg.level_minus_2.label, style.DEFAULTS["level_minus_2"]["label"])

    def test_whitespace_rejected_every_field(self):
        for field in style.LevelStyle.model_fields:
            for blank in ("", " ", "\u3000"):
                with self.subTest(field=field, blank=repr(blank)), self.assertRaises(ValidationError):
                    style.LevelStyle.model_validate({field: blank})

    def test_non_strings_rejected_every_field(self):
        for field in style.LevelStyle.model_fields:
            for value in (None, 42, True, [], b"hello"):
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    style.LevelStyle.model_validate({field: value})

    def test_overlong_rejected_every_field(self):
        for field, maximum in (("label", style.LABEL_MAX_LENGTH),
                               ("private_hint", style.HINT_MAX_LENGTH),
                               ("group_hint", style.HINT_MAX_LENGTH),
                               ("avoid", style.AVOID_MAX_LENGTH)):
            self.assertEqual(len(style.LevelStyle.model_validate({field: "字" * maximum}).model_dump()[field]), maximum)
            with self.subTest(field=field), self.assertRaises(ValidationError):
                style.LevelStyle.model_validate({field: "字" * (maximum + 1)})

    def test_control_characters_rejected(self):
        for field in style.LevelStyle.model_fields:
            for char in ("\t", "\r", "\r\n", "\0", "\x7f", "\x85", "\u202e", "\u200b", "\u2028", "\ud800"):
                with self.subTest(field=field, char=repr(char)), self.assertRaises(ValidationError):
                    style.LevelStyle.model_validate({field: "文字" + char})

    def test_assignment_validation(self):
        cfg = style.default_styles()
        with self.assertRaises(ValidationError):
            cfg.level_0.label = " "
        with self.assertRaises(ValidationError):
            cfg.level_0.private_hint = "x\ry"
        self.assertEqual(cfg.level_0.label, style.DEFAULTS["level_0"]["label"])

    def test_private_all_eight_levels_finite(self):
        cfg = style.default_styles()
        for level in range(-2, 6):
            result = style.get_style(level, True, cfg)
            self.assertTrue(result)
            self.assertLessEqual(len(result), style.MAX_RENDER_LENGTH)
            self.assertIn(style.HARD_BOUNDARY_REMINDER, result)

    def test_group_high_levels_fail_closed_not_clamped(self):
        for level in (3, 4, 5):
            self.assertEqual(style.get_style(level, False, style.default_styles()), "")

    def test_group_high_levels_require_explicit_strict_bool(self):
        cfg = style.default_styles()
        for level in (3, 4, 5):
            self.assertEqual(style.get_style(level, False, cfg, allow_group_high_levels=True),
                             style.get_style(level, True, cfg))
            self.assertEqual(style.get_style(level, False, cfg, allow_group_high_levels=False), "")
        for invalid in (1, 0, "true", "false", None, [], {}):
            for private in (True, False):
                self.assertEqual(style.get_style(3, private, cfg, allow_group_high_levels=invalid), "")

    def test_legacy_group_preserved_verbatim_but_absent_from_schema(self):
        legacy = "  旧群文案\n保留末尾空格  "
        cfg = style.StyleConfig.model_validate({"level_2": {"private_hint": "统一设定",
                                                          "group_hint": legacy}})
        dumped = cfg.model_dump()
        self.assertEqual(dumped["level_2"]["group_hint"], legacy)
        restored = style.StyleConfig.model_validate_json(cfg.model_dump_json())
        self.assertEqual(restored.model_dump(), dumped)
        for private in (True, False):
            text = style.get_style(2, private, restored)
            self.assertIn("统一设定", text)
            self.assertNotIn(legacy, text)
        sections = generate_plugin_config_schema(style.StyleConfig)["sections"]
        for section in sections.values():
            self.assertNotIn("group_hint", section["fields"])
        self.assertNotIn("group_hint", style.LevelStyle.model_fields)

    def test_unknown_extras_still_ignored(self):
        cfg = style.StyleConfig.model_validate({"unknown_root": "discard",
                                               "level_2": {"unknown_nested": "discard",
                                                           "group_hint": "旧群文案"}})
        self.assertNotIn("unknown_root", cfg.model_dump())
        self.assertNotIn("unknown_nested", cfg.level_2.model_dump())
        with self.assertRaises(ValueError):
            cfg.level_2.unknown_assigned = "discard"
        self.assertNotIn("unknown_assigned", cfg.level_2.model_dump())

    def test_legacy_group_validation_rejects_invalid_values(self):
        for value in (None, 1, True, "", "  ", "\n", "控制\r字符", "x" * 1201):
            with self.subTest(value=repr(value)), self.assertRaises(ValidationError):
                style.LevelStyle.model_validate({"group_hint": value})
        cfg = style.default_styles()
        with self.assertRaises(ValidationError):
            cfg.level_2.group_hint = "非法\t文本"

    def test_guard_is_single_internal_guidance(self):
        cfg = style.StyleConfig.model_validate({"level_0": {"private_hint": "统一设定", "avoid": "避免事项"}})
        text = style.get_style(0, False, cfg)
        self.assertEqual(text.count(style.BOUNDARY_TEXT), 1)
        self.assertEqual(style.BOUNDARY_TEXT, "仅描述相处方式，不改变身份权限；按当前对象及其意愿使用。")
        self.assertIn("never send its text as a reply", style.get_style.__doc__)

    def test_group_and_private_use_same_canonical_text(self):
        cfg = style.default_styles()
        cfg.level_2.private_hint = "私聊独有内容"
        cfg.level_2.group_hint = "群聊独有内容"
        public = style.get_style(2, False, cfg)
        self.assertNotIn("群聊独有内容", public)
        self.assertIn("私聊独有内容", public)
        self.assertEqual(public, style.get_style(2, True, cfg))
        self.assertIn("私聊独有内容", style.get_style(2, True, cfg))

    def test_invalid_levels_and_private_types(self):
        for level in (True, False, -3, 6, 2.0, "2", None):
            self.assertEqual(style.get_style(level, True, style.default_styles()), "")
        for private in (1, 0, "private", None):
            self.assertEqual(style.get_style(1, private, style.default_styles()), "")

    def test_defaults_not_shared_and_constant_immutable(self):
        first, second = style.default_styles(), style.default_styles()
        first.level_1.label = "仅修改第一份"
        self.assertNotEqual(first.level_1.label, second.level_1.label)
        dumped = second.model_dump()
        dumped["level_1"]["label"] = "仅修改导出字典"
        self.assertEqual(second.level_1.label, style.DEFAULTS["level_1"]["label"])
        self.assertEqual(style.restore_defaults().level_1.label, second.level_1.label)
        with self.assertRaises(TypeError):
            style.DEFAULTS["level_1"]["label"] = "不可修改"
        with self.assertRaises(TypeError):
            style.DEFAULTS["new"] = {}

    def test_renderer_pure_does_not_mutate_mapping(self):
        data = style.default_styles().model_dump()
        before = repr(data)
        self.assertTrue(style.get_style(0, True, data))
        self.assertEqual(repr(data), before)

    def test_renderer_revalidates_bypassed_model(self):
        cfg = style.default_styles()
        invalid = cfg.level_0.model_copy(update={"private_hint": "x" * 5000})
        bypassed = cfg.model_copy(update={"level_0": invalid})
        self.assertEqual(style.get_style(0, True, bypassed), "")
        self.assertEqual(style.get_style(0, True, {"level_0": {"label": "\n"}}), "")
        self.assertEqual(style.get_style(0, True, None), "")

    def test_fixed_reminder_cannot_be_configured_away(self):
        cfg = style.StyleConfig.model_validate({"HARD_BOUNDARY_REMINDER": "", "stop": False,
                                               "level_4": {"private_hint": "自定义语气"}})
        result = style.get_style(4, True, cfg)
        self.assertIn("自定义语气", result)
        self.assertIn(style.HARD_BOUNDARY_REMINDER, result)
        self.assertNotIn("HARD_BOUNDARY_REMINDER", style.StyleConfig.model_fields)

    def test_default_intent(self):
        self.assertIn("不泄隐私", style.DEFAULTS["level_minus_2"]["avoid"])
        self.assertIn("不翻无关旧账", style.DEFAULTS["level_minus_1"]["avoid"])
        self.assertIn("基础人格", style.DEFAULTS["level_3"]["private_hint"])
        self.assertIn("亲近", style.DEFAULTS["level_4"]["private_hint"])
        self.assertEqual(style.DEFAULTS["level_5"]["label"], "管理员")
        self.assertIn("不授予额外工具权限", style.DEFAULTS["level_5"]["avoid"])
        self.assertIn("不泄露", style.DEFAULTS["level_5"]["avoid"])

    def test_extra_settings_schema_labels_privacy_and_limits(self):
        sections = generate_plugin_config_schema(style.StyleConfig)["sections"]
        for section in sections.values():
            for field, label, maximum in (("private_hint", "额外设定", 1200),
                                          ("avoid", "避免事项", 600)):
                item = section["fields"][field]
                self.assertEqual(item["label"], label)
                self.assertEqual(item["max_length"], maximum)
                self.assertIn("额外设定", item["description"])
                self.assertIn("勿填个人秘密", item["description"])
        self.assertEqual(style.MAX_RENDER_LENGTH, 2200)

    def test_maximum_sized_extra_settings_render_without_truncation(self):
        cfg = style.default_styles()
        cfg.level_2.label = "名" * 48
        cfg.level_2.private_hint = "私" * 1200
        cfg.level_2.group_hint = "群" * 1200
        cfg.level_2.avoid = "避" * 600
        for private, marker in ((True, "私"), (False, "私")):
            result = style.get_style(2, private, cfg)
            self.assertTrue(result.startswith("[关系额外设定："))
            self.assertIn(marker * 1200, result)
            self.assertIn("避" * 600, result)
            self.assertIn(style.HARD_BOUNDARY_REMINDER, result)
            self.assertLessEqual(len(result), 2200)

    def test_existing_complete_configuration_is_not_replaced(self):
        existing = {name: {"label": "原有名称", "private_hint": "原有私聊内容",
                           "group_hint": "原有群聊内容", "avoid": "原有避免事项"}
                    for name in style.DEFAULTS}
        original = repr(existing)
        cfg = style.StyleConfig.model_validate(existing)
        self.assertEqual(cfg.model_dump(), existing)
        self.assertEqual(repr(existing), original)
        self.assertIn("原有私聊内容", style.get_style(3, True, cfg))

    def test_default_snapshot_matches_public_template(self):
        import tomllib
        approved = tomllib.loads((ROOT / "config.default.toml").read_text(encoding="utf-8"))
        for name, values in style.DEFAULTS.items():
            for field in ("label", "private_hint", "avoid"):
                self.assertEqual(values[field], approved[name][field])


if __name__ == "__main__":
    unittest.main()
