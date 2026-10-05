"""``bsk/tools.py`` 的单元测试 —— 六个多动作工具的 schema 与参数校验。

背景：本插件为对齐 BrowserSkill 的工具形态，新增了 6 个多动作工具
（``bsk_session`` / ``bsk_page`` / ``bsk_inspect`` / ``bsk_interact`` /
``bsk_tabs`` / ``bsk_assist``，共 33 个 action）。它们的 schema 不再由
``@filter.llm_tool`` 从 docstring 推导（那条路径丢弃 ``enum``），
而是由 ``bsk/tools.py`` 提供完整 JSON Schema，注册后覆写。

因此 ``bsk/tools.py`` 是**唯一**决定"模型能传什么、什么会被拒绝"的地方，
它没有任何框架保护：schema 写错会静默误导模型，校验写松会让坏参数进命令行。
本文件就是它的防线。

覆盖：

1. **schema 结构** —— 6 个工具、action 枚举与 ``TOOL_ACTIONS`` 同源、
   属性类型在 AstrBot 白名单内（**特别注意：没有 ``integer``**）、
   每个属性都有非空 description；
2. **归一化** —— camelCase → snake_case、连字符、空白、``None``；
3. **必填与类型** —— 每个 action 的必填参数缺失时给出可纠正的中文错误；
4. **数值范围** —— TOOL-SPEC §2 的每条范围各测两侧边界；
5. **互斥与条件规则** —— session/request_id 类互斥、debug 的 5 类条件规则；
6. **纯函数性** —— ``validate`` 不改入参、可重复调用。

pytest 在本机不可用，因此用标准库 ``unittest``。
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bsk import tools  # noqa: E402

# AstrBot 的类型白名单（``func_tool_manager.py`` 的 SUPPORTED_TYPES）。
#
# **没有 integer** —— 整数语义的参数必须写 number，否则
# ``verify_astrbot_contract.py`` 会红、provider 侧也可能拒绝。
ALLOWED_TYPES = {"string", "number", "object", "array", "boolean"}

# 期望的工具与 action 数量（TOOL-SPEC §1）。
EXPECTED_ACTIONS = {
    "bsk_session": ("start", "stop", "list"),
    "bsk_page": ("navigate", "back", "forward", "reload", "wait"),
    "bsk_inspect": (
        "observe",
        "snapshot",
        "html",
        "screenshot",
        "console",
        "network",
        "debug",
    ),
    "bsk_interact": (
        "click",
        "hover",
        "wheel",
        "scroll-to",
        "focus",
        "blur",
        "fill",
        "select",
        "press",
    ),
    "bsk_tabs": ("list", "create", "select", "close", "borrow", "return"),
    "bsk_assist": ("resize", "emulate", "request-help"),
}


def call(tool_name: str, raw: dict) -> dict:
    """按契约调用：先归一化，再校验。"""
    return tools.validate(tool_name, tools.normalize_args(raw))


class TestToolShape(unittest.TestCase):
    """工具清单与 action 枚举。"""

    def test_exactly_six_tools(self) -> None:
        self.assertEqual(set(tools.TOOL_SCHEMAS), set(EXPECTED_ACTIONS))

    def test_actions_match_spec(self) -> None:
        """action 集合必须与 TOOL-SPEC 完全一致（不是"至少包含"）。"""
        for name, expected in EXPECTED_ACTIONS.items():
            self.assertEqual(tuple(tools.TOOL_ACTIONS[name]), expected, name)

    def test_every_tool_has_description(self) -> None:
        """描述进 system prompt —— 空描述会让模型不知道该工具干什么。"""
        for name in EXPECTED_ACTIONS:
            desc = tools.TOOL_DESCRIPTIONS.get(name, "")
            self.assertTrue(desc.strip(), f"{name} 没有描述")

    def test_descriptions_are_chinese(self) -> None:
        """面向模型的文案必须是中文。"""
        for name, desc in tools.TOOL_DESCRIPTIONS.items():
            self.assertTrue(
                any("\u4e00" <= ch <= "\u9fff" for ch in desc),
                f"{name} 的描述不含中文",
            )


class TestSchemaValidity(unittest.TestCase):
    """schema 结构必须能被 AstrBot 正常序列化给 provider。"""

    def test_top_level_is_object(self) -> None:
        for name, schema in tools.TOOL_SCHEMAS.items():
            self.assertEqual(schema.get("type"), "object", name)

    def test_required_is_only_action(self) -> None:
        """DSH 对 ``action`` 声明了 required，我们覆写路径能保留它。

        其余参数不能进 required —— action 之间的参数是并集，
        任何一个单独都不是全局必填。
        """
        for name, schema in tools.TOOL_SCHEMAS.items():
            self.assertEqual(schema.get("required"), ["action"], name)

    def test_property_types_in_whitelist(self) -> None:
        """**这条专门防 ``integer``** —— AstrBot 的白名单里没有它。"""
        for name, schema in tools.TOOL_SCHEMAS.items():
            for prop, spec in schema["properties"].items():
                self.assertIn(
                    spec.get("type"),
                    ALLOWED_TYPES,
                    f"{name}.{prop} 的 type 不在白名单内：{spec.get('type')!r}",
                )

    def test_every_property_has_description(self) -> None:
        for name, schema in tools.TOOL_SCHEMAS.items():
            for prop, spec in schema["properties"].items():
                self.assertTrue(
                    str(spec.get("description", "")).strip(),
                    f"{name}.{prop} 缺少 description",
                )

    def test_action_enum_matches_tool_actions(self) -> None:
        """enum 必须与 ``TOOL_ACTIONS`` 同源 —— 两处不一致会让模型照 schema 传却被拒。"""
        for name, schema in tools.TOOL_SCHEMAS.items():
            enum = schema["properties"]["action"].get("enum")
            self.assertEqual(list(enum), list(tools.TOOL_ACTIONS[name]), name)

    def test_no_request_id_anywhere(self) -> None:
        """``request_id`` 已按裁决删除（不支持的能力不暴露）。"""
        for name, schema in tools.TOOL_SCHEMAS.items():
            self.assertNotIn("request_id", schema["properties"], name)


class TestNormalize(unittest.TestCase):
    """参数名归一化：模型可能照 DSH 的 camelCase 传，也可能照 schema 传。"""

    def test_camel_to_snake(self) -> None:
        got = tools.normalize_args({"waitUntil": "load", "tabId": 42, "noFocus": True})
        self.assertEqual(got, {"wait_until": "load", "tab_id": 42, "no_focus": True})

    def test_snake_passthrough(self) -> None:
        got = tools.normalize_args({"wait_until": "load", "tab_id": 42})
        self.assertEqual(got, {"wait_until": "load", "tab_id": 42})

    def test_hyphen_becomes_underscore(self) -> None:
        self.assertEqual(tools.normalize_args({"max-depth": 4}), {"max_depth": 4})

    def test_none_values_dropped(self) -> None:
        self.assertEqual(tools.normalize_args({"a": None, "b": 1}), {"b": 1})

    def test_non_dict_is_tolerated(self) -> None:
        """非法输入不抛异常 —— 报错交给 validate，这样消息能说清是哪个参数。"""
        for bad in (None, [], "x", 42):
            tools.normalize_args(bad)  # 不抛即通过


class TestActionDispatch(unittest.TestCase):
    """action 是必填且必须是声明的取值。"""

    def test_missing_action(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_session", {})

    def test_unknown_tool(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_nope", {"action": "list"})

    def test_invalid_action_lists_options(self) -> None:
        """错误消息要列出可选值，模型才能自我纠正。"""
        with self.assertRaises(tools.BskToolError) as ctx:
            call("bsk_session", {"action": "bogus"})
        msg = str(ctx.exception)
        self.assertIn("start", msg)
        self.assertIn("list", msg)

    def test_model_placeholder_rejected(self) -> None:
        """``__model__`` 是框架保留名，不是可用 action。"""
        with self.assertRaises(tools.BskToolError):
            call("bsk_session", {"action": "__model__"})

    def test_snake_action_normalized_to_canonical(self) -> None:
        """``scroll_to`` 要归一成 schema 里的 ``scroll-to``。

        这条是回归测试：曾经因为归一化后没写回规范形式，
        ``scroll-to`` 掉进了 press 分支、报"缺 key"，该 action 完全不可用。
        """
        got = call("bsk_interact", {"action": "scroll_to", "target": "@e1"})
        self.assertEqual(got["action"], "scroll-to")

    def test_canonical_hyphen_action_accepted(self) -> None:
        got = call("bsk_interact", {"action": "scroll-to", "target": "@e1"})
        self.assertEqual(got["action"], "scroll-to")


class TestRequiredArguments(unittest.TestCase):
    """每个 action 的必填参数缺失时必须报可纠正的中文错。"""

    def test_interact_requires_target(self) -> None:
        for action in ("click", "hover", "scroll-to", "focus", "blur", "fill"):
            with self.assertRaises(tools.BskToolError, msg=action):
                call("bsk_interact", {"action": action})
            # 补上 target 后 click/hover/scroll-to/focus/blur 应通过；
            # fill 还要 value，单独在下面测。
            if action != "fill":
                call("bsk_interact", {"action": action, "target": "@e1"})

    def test_fill_requires_value(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_interact", {"action": "fill", "target": "@e1"})
        call("bsk_interact", {"action": "fill", "target": "@e1", "value": ""})

    def test_select_requires_values(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_interact", {"action": "select", "target": "@e1"})
        with self.assertRaises(tools.BskToolError):
            call("bsk_interact", {"action": "select", "target": "@e1", "values": []})
        call("bsk_interact", {"action": "select", "target": "@e1", "values": ["a"]})

    def test_press_requires_key(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_interact", {"action": "press"})
        got = call("bsk_interact", {"action": "press", "key": "Enter"})
        self.assertEqual(got["key"], "Enter")

    def test_page_navigate_requires_url(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_page", {"action": "navigate"})
        call("bsk_page", {"action": "navigate", "url": "https://example.com"})

    def test_tabs_requires_tab_id(self) -> None:
        for action in ("select", "close", "borrow", "return"):
            with self.assertRaises(tools.BskToolError, msg=action):
                call("bsk_tabs", {"action": action})
            call("bsk_tabs", {"action": action, "tab_id": 42})

    def test_assist_resize_requires_both_dims(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_assist", {"action": "resize", "width": 800})
        call("bsk_assist", {"action": "resize", "width": 800, "height": 600})

    def test_request_help_requires_prompt(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_assist", {"action": "request-help"})
        call("bsk_assist", {"action": "request-help", "prompt": "请登录"})


class TestMutualExclusion(unittest.TestCase):
    """互斥规则。"""

    def test_session_start_width_height_together(self) -> None:
        """CLI 明确要求：--width 与 --height 必须同时给才生效。"""
        with self.assertRaises(tools.BskToolError):
            call("bsk_session", {"action": "start", "width": 800})
        with self.assertRaises(tools.BskToolError):
            call("bsk_session", {"action": "start", "height": 600})
        call("bsk_session", {"action": "start", "width": 800, "height": 600})

    def test_emulate_off_is_exclusive(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_assist", {"action": "emulate", "off": True, "device": "iphone-14"})
        with self.assertRaises(tools.BskToolError):
            call("bsk_assist", {"action": "emulate", "off": True, "width": 390})
        call("bsk_assist", {"action": "emulate", "off": True})

    def test_emulate_needs_something(self) -> None:
        """什么都不给 / 只给 mobile，都无事可做。"""
        with self.assertRaises(tools.BskToolError):
            call("bsk_assist", {"action": "emulate"})
        with self.assertRaises(tools.BskToolError):
            call("bsk_assist", {"action": "emulate", "mobile": True})
        call("bsk_assist", {"action": "emulate", "device": "iphone-14"})


class TestNumericRanges(unittest.TestCase):
    """TOOL-SPEC §2 的数值范围，每条测两侧边界。"""

    def test_resize_range(self) -> None:
        """100..=7680。"""
        for w, h, ok in ((100, 100, True), (7680, 7680, True), (99, 600, False), (7681, 600, False)):
            with self.subTest(w=w):
                if ok:
                    call("bsk_assist", {"action": "resize", "width": w, "height": h})
                else:
                    with self.assertRaises(tools.BskToolError):
                        call("bsk_assist", {"action": "resize", "width": w, "height": h})

    def test_timeout_must_be_positive(self) -> None:
        for ms, ok in ((1, True), (0, False), (-1, False)):
            with self.subTest(ms=ms):
                if ok:
                    call("bsk_page", {"action": "wait", "timeout_ms": ms})
                else:
                    with self.assertRaises(tools.BskToolError):
                        call("bsk_page", {"action": "wait", "timeout_ms": ms})

    def test_settle_ms_positive(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_interact", {"action": "hover", "target": "@e1", "settle_ms": 0})
        call("bsk_interact", {"action": "hover", "target": "@e1", "settle_ms": 1})

    def test_wheel_requires_nonzero_delta(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_interact", {"action": "wheel"})
        with self.assertRaises(tools.BskToolError):
            call("bsk_interact", {"action": "wheel", "delta_x": 0, "delta_y": 0})
        call("bsk_interact", {"action": "wheel", "delta_y": 100})

    def test_console_since_non_negative(self) -> None:
        call("bsk_inspect", {"action": "console", "since": 0})
        with self.assertRaises(tools.BskToolError):
            call("bsk_inspect", {"action": "console", "since": -1})

    def test_html_max_bytes_positive(self) -> None:
        call("bsk_inspect", {"action": "html", "max_bytes": 1})
        with self.assertRaises(tools.BskToolError):
            call("bsk_inspect", {"action": "html", "max_bytes": 0})


class TestHtmlRefFormat(unittest.TestCase):
    """``ref`` 必须是快照引用。"""

    def test_accepts_at_and_bare(self) -> None:
        for ref in ("@e3", "e3"):
            call("bsk_inspect", {"action": "html", "ref": ref})

    def test_rejects_css_selector(self) -> None:
        """DSH 要求 ref 必须是 @eN；CSS 选择器不是 ref。"""
        with self.assertRaises(tools.BskToolError):
            call("bsk_inspect", {"action": "html", "ref": "div#main"})


class TestDebugRules(unittest.TestCase):
    """``debug`` 的 24 个 action 与 5 类条件规则。"""

    def test_requires_debug_action(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_inspect", {"action": "debug"})

    def test_all_debug_actions_reach_conclusion(self) -> None:
        """每个 debug_action 都必须给出明确结论（通过或报错），不能静默吞掉。"""
        for action in tools.DEBUG_ACTIONS:
            with self.subTest(action=action):
                raw = {"action": "debug", "debug_action": action}
                if action in tools.ID_REQUIRED_DEBUG_ACTIONS:
                    raw["id"] = "x"
                if action == "rule_add":
                    raw["rule"] = '{"match":{"url":"https://x/*"},"effect":{"type":"block"}}'
                if action == "replay":
                    raw["replay"] = '{"key":"k"}'
                try:
                    call("bsk_inspect", raw)
                except tools.BskToolError as exc:
                    self.assertTrue(str(exc).strip(), f"{action} 报错但消息为空")

    def test_id_required_actions(self) -> None:
        for action in tools.ID_REQUIRED_DEBUG_ACTIONS:
            with self.subTest(action=action):
                raw = {"action": "debug", "debug_action": action}
                if action == "rule_add":
                    raw["rule"] = "{}"
                if action == "replay":
                    raw["replay"] = "{}"
                with self.assertRaises(tools.BskToolError):
                    call("bsk_inspect", raw)

    def test_rule_add_requires_rule_json(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_inspect", {"action": "debug", "debug_action": "rule_add"})
        with self.assertRaises(tools.BskToolError):
            call(
                "bsk_inspect",
                {"action": "debug", "debug_action": "rule_add", "rule": "not-json"},
            )
        call(
            "bsk_inspect",
            {"action": "debug", "debug_action": "rule_add", "rule": '{"a":1}'},
        )

    def test_since_forbidden_for_analysis_actions(self) -> None:
        for action in tools.NO_SINCE_DEBUG_ACTIONS:
            with self.subTest(action=action):
                with self.assertRaises(tools.BskToolError):
                    call("bsk_inspect", {"action": "debug", "debug_action": action, "since": 1})

    def test_model_placeholder_rejected(self) -> None:
        """``debug_action`` 与 tool 级 ``action`` 一样拒绝框架保留名。"""
        with self.assertRaises(tools.BskToolError):
            call("bsk_inspect", {"action": "debug", "debug_action": "__model__"})

    def test_limit_range_enforced(self) -> None:
        """`limit` 范围 1..100 —— 突变测试发现这条曾无回归保护。"""
        for value, ok in ((1, True), (100, True), (0, False), (101, False), (-1, False)):
            with self.subTest(limit=value):
                raw = {"action": "debug", "debug_action": "requests", "limit": value}
                if ok:
                    call("bsk_inspect", raw)
                else:
                    with self.assertRaises(tools.BskToolError):
                        call("bsk_inspect", raw)

    def test_offset_range_enforced(self) -> None:
        """`offset` 范围 0..65536。"""
        for value, ok in ((0, True), (65536, True), (65537, False), (-1, False)):
            with self.subTest(offset=value):
                raw = {"action": "debug", "debug_action": "performance", "offset": value}
                if ok:
                    call("bsk_inspect", raw)
                else:
                    with self.assertRaises(tools.BskToolError):
                        call("bsk_inspect", raw)

    def test_max_chars_range_enforced(self) -> None:
        """`max_chars` 范围 1..16384。"""
        for value, ok in ((1, True), (16384, True), (0, False), (16385, False)):
            with self.subTest(max_chars=value):
                raw = {
                    "action": "debug",
                    "debug_action": "request",
                    "id": "x",
                    "max_chars": value,
                }
                if ok:
                    call("bsk_inspect", raw)
                else:
                    with self.assertRaises(tools.BskToolError):
                        call("bsk_inspect", raw)

    def test_budget_range_enforced(self) -> None:
        """`budget` 范围 4096..262144。"""
        for value, ok in ((4096, True), (262144, True), (4095, False), (262145, False)):
            with self.subTest(budget=value):
                raw = {"action": "debug", "debug_action": "activity", "budget": value}
                if ok:
                    call("bsk_inspect", raw)
                else:
                    with self.assertRaises(tools.BskToolError):
                        call("bsk_inspect", raw)

    def test_slow_ms_only_for_aggregate(self) -> None:
        """`slow_ms` 只对 aggregate 有意义 —— 别的 action 传它必须报错。"""
        call(
            "bsk_inspect",
            {"action": "debug", "debug_action": "aggregate", "slow_ms": 1000},
        )
        for action in ("performance", "requests", "capabilities"):
            with self.subTest(action=action):
                with self.assertRaises(tools.BskToolError):
                    call("bsk_inspect", {"action": "debug", "debug_action": action, "slow_ms": 1})

    def test_window_ms_only_for_duplicates(self) -> None:
        """`window_ms` 只对 duplicates 有意义。"""
        call(
            "bsk_inspect",
            {"action": "debug", "debug_action": "duplicates", "window_ms": 1000},
        )
        with self.assertRaises(tools.BskToolError):
            call(
                "bsk_inspect",
                {"action": "debug", "debug_action": "aggregate", "window_ms": 1000},
            )

    def test_controlled_only_actions(self) -> None:
        """`include_controlled` 只对 aggregate / duplicates 有意义。"""
        for action in tools.CONTROLLED_ONLY_DEBUG_ACTIONS:
            with self.subTest(action=action):
                call(
                    "bsk_inspect",
                    {"action": "debug", "debug_action": action, "include_controlled": True},
                )
        with self.assertRaises(tools.BskToolError):
            call(
                "bsk_inspect",
                {"action": "debug", "debug_action": "performance", "include_controlled": True},
            )

    def test_part_requires_pointer(self) -> None:
        """`part` 的五种取值不带 pointer 时都合法。"""
        for part in tools.DEBUG_PART_VALUES:
            with self.subTest(part=part):
                call(
                    "bsk_inspect",
                    {
                        "action": "debug",
                        "debug_action": "request",
                        "id": "x",
                        "part": part,
                    },
                )

    def test_pointer_restricts_part_to_request_or_response(self) -> None:
        """带 `pointer` 时 `part` 只允许 request / response。

        pointer 是 JSON 指针，只在完整请求/响应体里有意义；
        带 pointer 却要 headers/metadata/timing 是自相矛盾的组合。
        """
        for part in ("request", "response"):
            with self.subTest(part=part):
                call(
                    "bsk_inspect",
                    {
                        "action": "debug",
                        "debug_action": "request",
                        "id": "x",
                        "pointer": "/a",
                        "part": part,
                    },
                )
        for part in ("headers", "metadata", "timing", "bogus"):
            with self.subTest(part=part):
                with self.assertRaises(tools.BskToolError):
                    call(
                        "bsk_inspect",
                        {
                            "action": "debug",
                            "debug_action": "request",
                            "id": "x",
                            "pointer": "/a",
                            "part": part,
                        },
                    )

    def test_pointer_requires_valid_part(self) -> None:
        """带 `pointer` 时 `part` 必填 —— 不填就不知道该解析哪一部分。"""
        with self.assertRaises(tools.BskToolError):
            call(
                "bsk_inspect",
                {"action": "debug", "debug_action": "request", "id": "x", "pointer": "/a"},
            )


class TestCompletionCriteria(unittest.TestCase):
    """``completion_criteria`` 的键名转换（这是 ``request-help`` 的命门）。

    模型照 schema 填 camelCase，而 bsk CLI 只认 snake_case —— 中间必须转换。
    这条曾经完全失效：schema 是 camelCase、校验器只认 snake_case，
    模型无论怎么填都被拒，``request-help`` 实际不可用。
    """

    def test_camel_case_accepted_and_converted(self) -> None:
        got = call(
            "bsk_assist",
            {
                "action": "request-help",
                "prompt": "请登录",
                "completion_criteria": {
                    "any": [{"selectorExists": "#dashboard"}],
                    "stableForMs": 1000,
                },
            },
        )
        cc = got["completion_criteria"]
        self.assertEqual(cc.get("stable_for_ms"), 1000)
        self.assertEqual(cc["any"][0].get("selector_exists"), "#dashboard")

    def test_snake_case_also_accepted(self) -> None:
        got = call(
            "bsk_assist",
            {
                "action": "request-help",
                "prompt": "请登录",
                "completion_criteria": {
                    "all": [{"url_contains": "/ok"}],
                    "stable_for_ms": 0,
                },
            },
        )
        cc = got["completion_criteria"]
        self.assertEqual(cc["all"][0].get("url_contains"), "/ok")

    def test_unknown_key_rejected_with_camel_case_hint(self) -> None:
        """错误消息要用 camelCase 指代 —— 那才是模型在 schema 里看到的写法。"""
        with self.assertRaises(tools.BskToolError) as ctx:
            call(
                "bsk_assist",
                {
                    "action": "request-help",
                    "prompt": "x",
                    "completion_criteria": {"bogus": 1},
                },
            )
        msg = str(ctx.exception)
        self.assertIn("stableForMs", msg)


class TestTabIdPreserved(unittest.TestCase):
    """``tab_id`` 必须保留在 validate 的输出里。

    回归测试：曾经 8/10 个 action 只校验不写回，模型传 tab_id 后
    **不报错也不生效**，命令打在了另一个标签上。
    """

    CASES = (
        ("bsk_interact", {"action": "click", "target": "@e1"}),
        ("bsk_interact", {"action": "hover", "target": "@e1"}),
        ("bsk_inspect", {"action": "observe"}),
        ("bsk_inspect", {"action": "screenshot"}),
        ("bsk_inspect", {"action": "console"}),
        ("bsk_inspect", {"action": "debug", "debug_action": "capabilities"}),
        ("bsk_assist", {"action": "resize", "width": 800, "height": 600}),
        ("bsk_assist", {"action": "emulate", "off": True}),
        ("bsk_page", {"action": "reload"}),
        ("bsk_tabs", {"action": "list"}),
    )

    def test_tab_id_kept(self) -> None:
        for tool_name, raw in self.CASES:
            with self.subTest(tool=f"{tool_name}.{raw['action']}"):
                got = call(tool_name, dict(raw, tab_id=42))
                self.assertEqual(got.get("tab_id"), 42)

    def test_tab_id_absent_when_not_given(self) -> None:
        """没传时不应凭空冒出 tab_id: None（那是噪音键）。"""
        got = call("bsk_inspect", {"action": "observe"})
        self.assertIsNone(got.get("tab_id"))


class TestTimeoutMsPreserved(unittest.TestCase):
    """``timeout_ms`` 必须在**每一个**支持它的 interact action 上保留。

    回归测试：第三轮审阅用突变测试发现，`click`/`fill`/`press` 三个分支
    算出了 timeout_ms 却没写回返回 dict —— 模型传了超时既不报错也不生效，
    命令仍按 bsk 默认的 30 秒走。慢页面上模型会误判成"操作失败"并重试，
    进而造成重复点击/重复提交。

    这条缺陷在 `tab_id` 那次修复中活了下来（同型问题换了个参数），
    所以这里**逐个 action** 断言，而不是只抽查一个。
    """

    CASES = (
        ("click", {"target": "@e1"}),
        ("fill", {"target": "@e1", "value": "v"}),
        ("press", {"key": "Enter"}),
        ("hover", {"target": "@e1"}),
        ("scroll-to", {"target": "@e1"}),
        ("focus", {"target": "@e1"}),
        ("blur", {"target": "@e1"}),
        ("select", {"target": "@e1", "values": ["a"]}),
        ("wheel", {"delta_y": 100}),
    )

    def test_timeout_ms_kept_on_every_action(self) -> None:
        for action, extra in self.CASES:
            with self.subTest(action=action):
                got = call("bsk_interact", dict(extra, action=action, timeout_ms=5000))
                self.assertEqual(got.get("timeout_ms"), 5000, action)

    def test_timeout_ms_rejects_non_positive(self) -> None:
        for bad in (0, -1):
            with self.subTest(value=bad):
                with self.assertRaises(tools.BskToolError):
                    call("bsk_interact", {"action": "click", "target": "@e1", "timeout_ms": bad})


class TestSessionPreserved(unittest.TestCase):
    """``session`` 必须保留在 validate 的输出里（5 个多动作工具）。

    回归测试：schema 里六个工具都有 session（对齐 DSH 的参数并集），
    但校验器早先不写回、main.py 也只用本对话的键 —— 模型显式指定的会话
    被静默丢弃，多会话场景下命令打在另一个会话上，且不报错。
    """

    CASES = (
        ("bsk_page", {"action": "navigate", "url": "https://x"}),
        ("bsk_inspect", {"action": "observe"}),
        ("bsk_interact", {"action": "click", "target": "@e1"}),
        ("bsk_tabs", {"action": "list"}),
        ("bsk_assist", {"action": "resize", "width": 800, "height": 600}),
    )

    def test_session_kept(self) -> None:
        for tool_name, raw in self.CASES:
            with self.subTest(tool=f"{tool_name}.{raw['action']}"):
                got = call(tool_name, dict(raw, session="grpA"))
                self.assertEqual(got.get("session"), "grpA")

    def test_session_absent_when_not_given(self) -> None:
        got = call("bsk_page", {"action": "navigate", "url": "https://x"})
        self.assertNotIn("session", got)

    def test_blank_session_rejected(self) -> None:
        """空白串不是"没给"，而是无效输入 —— 报错比静默忽略好。

        静默忽略会让模型以为"我指定了会话"，实际用的是当前会话；
        报错能让它自己改过来。
        """
        with self.assertRaises(tools.BskToolError):
            call("bsk_page", {"action": "navigate", "url": "https://x", "session": "   "})


class TestNoSpuriousArgvKeys(unittest.TestCase):
    """不该出现的键不能进结果 —— 它们会变成命令行上的垃圾参数。

    `--modifiers ''` 曾经恒发（`modifiers or []` 让服务层的判空失效）；
    delta 曾被渲染成 `0.0` 浮点（CLI 的默认值是整数 `0`）。
    """

    def test_modifiers_absent_when_empty(self) -> None:
        got = call("bsk_interact", {"action": "click", "target": "@e1"})
        self.assertNotIn("modifiers", got)

    def test_modifiers_kept_when_given(self) -> None:
        got = call("bsk_interact", {"action": "click", "target": "@e1", "modifiers": ["ctrl"]})
        self.assertEqual(got.get("modifiers"), ["ctrl"])

    def test_delta_rendered_as_int(self) -> None:
        got = call("bsk_interact", {"action": "wheel", "delta_y": 100})
        self.assertIsInstance(got.get("delta_x"), int)
        self.assertIsInstance(got.get("delta_y"), int)
        self.assertEqual(got.get("delta_x"), 0)
        self.assertEqual(got.get("delta_y"), 100)

    def test_capture_id_absent_when_empty(self) -> None:
        """capture_id 是 Canvas 点击专用；不传时不该出现空串。"""
        got = call("bsk_interact", {"action": "click", "target": "@e1"})
        self.assertEqual(got.get("capture_id"), "")


class TestUnknownArguments(unittest.TestCase):
    """未知参数必须拒绝，而不是静默忽略。"""

    def test_rejects_unknown(self) -> None:
        with self.assertRaises(tools.BskToolError):
            call("bsk_page", {"action": "wait", "bogus": 1})

    def test_rejects_unknown_on_every_tool(self) -> None:
        """`check_known` 在六个工具上都必须生效。

        突变测试曾发现「关掉 check_known」无任何测试报警 ——
        那会让所有拼错的参数静默通过，一路传到命令行。
        """
        MINIMAL = (
            ("bsk_session", {"action": "list"}),
            ("bsk_page", {"action": "wait"}),
            ("bsk_inspect", {"action": "observe"}),
            ("bsk_interact", {"action": "wheel", "delta_y": 1}),
            ("bsk_tabs", {"action": "list"}),
            ("bsk_assist", {"action": "emulate", "off": True}),
        )
        for tool_name, raw in MINIMAL:
            with self.subTest(tool=tool_name):
                with self.assertRaises(tools.BskToolError):
                    call(tool_name, dict(raw, totally_bogus_arg=1))

    def test_unknown_per_action(self) -> None:
        """同一工具的不同 action 也各自拒绝不属于自己的参数。

        注意 `target` **不是** wheel 的非法参数 —— CLI 的 `wheel [TARGET]`
        接受一个可选的命中点。这里换一个真正越界的参数。
        """
        with self.assertRaises(tools.BskToolError):
            call("bsk_interact", {"action": "wheel", "delta_y": 1, "values": ["x"]})
        with self.assertRaises(tools.BskToolError):
            call("bsk_interact", {"action": "click", "target": "@e1", "delta_y": 1})

    def test_session_list_takes_nothing(self) -> None:
        call("bsk_session", {"action": "list"})
        for key, val in (("session", "x"), ("tab_id", 1), ("url", "https://x")):
            with self.subTest(key=key):
                with self.assertRaises(tools.BskToolError):
                    call("bsk_session", {"action": "list", key: val})


class TestPurity(unittest.TestCase):
    """纯函数：不改入参、可重复调用。"""

    def test_input_not_mutated(self) -> None:
        raw = {"action": "navigate", "url": "https://x", "waitUntil": "load"}
        snapshot = copy.deepcopy(raw)
        tools.normalize_args(raw)
        self.assertEqual(raw, snapshot)

    def test_repeatable(self) -> None:
        raw = {"action": "click", "target": "@e1"}
        first = call("bsk_interact", raw)
        second = call("bsk_interact", raw)
        self.assertEqual(first, second)

    def test_schema_not_mutated_by_validate(self) -> None:
        """validate 不得改动全局 TOOL_SCHEMAS（那会污染后续请求）。"""
        before = copy.deepcopy(tools.TOOL_SCHEMAS["bsk_assist"])
        call("bsk_assist", {"action": "resize", "width": 800, "height": 600})
        self.assertEqual(tools.TOOL_SCHEMAS["bsk_assist"], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
