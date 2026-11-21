"""bsk 0.4.x / 0.5.0 协作控制协议适配与错误细分的单元测试和端到端集成测试。

覆盖范围：
1. ``bsk/cooperation.py`` 纯逻辑层：能力探测、凭证解析（顶层 ``observation_id``
   与嵌套 ``observation.observation_id`` 严格隔离）、命令分类、目标 tab 提取、
   ref 检测、参数拼接与内嵌观察提取；
2. ``bsk/errors.py`` 与 ``bsk/runner.py``：``permission_denied`` 按 ``data.reason``
   精准细分（不再一刀切报窗口归属错误）、``effect_state="unknown"``、
   ``user_aborted`` / ``user_moved_tab`` / ``dialog_pending`` / 目标 ``not_found``
   不误触发整会话重建；
3. ``BskService`` + ``SessionManager`` 端到端闭环：
   - Issue #7 场景复现（``bsk 0.5.0`` target 策略下点击与全页截图）；
   - 只读严格命令（全页截图）自动重同步重试一次，写命令与 ``evaluate`` 绝不盲目重试；
   - 安全防线（人工接管、用户停止、``effect_state="unknown"``、过期 ``@eN`` 防误点）；
   - 版本兼容矩阵（``bsk <= 0.3.3`` / ``0.4.0`` / ``0.5.0`` / 新 CLI 配旧扩展降级）；
   - 多标签页与窗口上下文隔离、Canvas 视觉点击、``session_id`` 归并防重复建会话。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bsk import errors  # noqa: E402
from bsk.config import parse_settings  # noqa: E402
from bsk.cooperation import (  # noqa: E402
    COOP_MODE_NONE,
    COOP_MODE_STRICT,
    COOP_MODE_STRICT_BOOTSTRAP,
    COOP_MODE_TARGETED,
    CooperationCapability,
    CoopReceipt,
    attach_coop_flags,
    classify_command_coop,
    command_invalidates_tab_context,
    command_invalidates_window_context,
    command_leaves_blank_tab,
    command_switches_default_tab,
    extract_sync_observation,
    extract_target_tab_id,
    has_snapshot_ref_target,
    is_readonly_strict_command,
)
from bsk.errors import (  # noqa: E402
    CODE_DIALOG_PENDING,
    CODE_NOT_FOUND,
    CODE_PERMISSION_DENIED,
    CODE_UNSUPPORTED,
    CODE_USER_ABORTED,
    EXIT_BROWSER,
    EXIT_DIALOG_PENDING,
    EXIT_PROTOCOL,
    EXIT_TIMEOUT,
    EXIT_USER_ERROR,
    BskBrowserError,
    BskError,
    BskOutcomeUnknown,
    BskProtocolError,
    BskSessionGone,
    BskTimeout,
)
from bsk.models import BskResult  # noqa: E402
from bsk.runner import _extract_effect_state, _extract_error_fields  # noqa: E402
from bsk.service import BskService  # noqa: E402
from bsk.session import SessionManager  # noqa: E402

# ----------------------------------------------------------------------
# 帮助文本夹具（与真实 bsk CLI 输出对齐）
# ----------------------------------------------------------------------

_COOP_HELP_V050 = """Cooperate with a person on the same browser tab

Usage: bsk cooperate [OPTIONS] --session <SESSION> <ACTION>

Arguments:
  <ACTION>  [possible values: enable, status, sync, wait]

Options:
      --context <CONTEXT>
      --policy <POLICY>          [possible values: auto, strict, target]
      --observation <OBSERVATION>
      --settle
      --json
"""

_COOP_HELP_V040 = """Cooperate with a person on the same browser tab

Usage: bsk cooperate [OPTIONS] --session <SESSION> <ACTION>

Arguments:
  <ACTION>  [possible values: enable, status, sync, wait]

Options:
      --context <CONTEXT>
      --settle
      --json
"""

_MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ----------------------------------------------------------------------
# 1. bsk/cooperation.py 纯逻辑单测
# ----------------------------------------------------------------------


class TestCooperationCapability(unittest.TestCase):
    """``CooperationCapability.from_help_text`` 的版本探测矩阵。"""

    def test_v050_help_enables_all_features(self) -> None:
        cap = CooperationCapability.from_help_text(_COOP_HELP_V050)
        self.assertTrue(cap.supported)
        self.assertTrue(cap.supports_observation)
        self.assertTrue(cap.supports_settle)
        self.assertTrue(cap.supports_policy)

    def test_v040_help_lacks_observation_and_policy(self) -> None:
        cap = CooperationCapability.from_help_text(_COOP_HELP_V040)
        self.assertTrue(cap.supported)
        self.assertFalse(cap.supports_observation)
        self.assertTrue(cap.supports_settle)
        self.assertFalse(cap.supports_policy)

    def test_legacy_or_empty_help_returns_unsupported(self) -> None:
        for text in (
            "",
            "error: unrecognized subcommand 'cooperate'",
            "Usage: bsk sync",  # 缺少 --context
            "Usage: bsk --context <CTX>",  # 缺少 sync
        ):
            with self.subTest(text=text):
                cap = CooperationCapability.from_help_text(text)
                self.assertFalse(cap.supported)
                self.assertFalse(cap.supports_observation)


class TestCoopReceipt(unittest.TestCase):
    """``CoopReceipt.from_sync_json`` 的解析与凭证隔离语义。"""

    def test_extracts_top_level_observation_id_never_nested(self) -> None:
        """顶层 observation_id 与嵌套 observation.observation_id 必须严格区分。"""
        payload = {
            "kind": "baseline",
            "tab_id": 12,
            "context_id": "ctx_top_1",
            "observation_id": "obs_top_1",
            "control_policy": "target",
            "observation": {
                "observation_id": "nested_ref_obs_DO_NOT_USE",
                "text": "RootWebArea",
            },
        }
        receipt = CoopReceipt.from_sync_json(payload)
        assert receipt is not None
        self.assertEqual(receipt.context_id, "ctx_top_1")
        self.assertEqual(receipt.observation_id, "obs_top_1")
        self.assertTrue(receipt.is_target_policy)
        self.assertEqual(receipt.tab_id, 12)
        self.assertEqual(receipt.kind, "baseline")
        self.assertTrue(receipt.has_valid_context())
        self.assertTrue(receipt.has_valid_observation())

    def test_missing_top_level_observation_does_not_fall_back_to_nested(self) -> None:
        payload = {
            "kind": "baseline",
            "context_id": "ctx_only",
            "control_policy": "strict",
            "observation": {
                "observation_id": "nested_ref_obs_DO_NOT_USE",
                "text": "RootWebArea",
            },
        }
        receipt = CoopReceipt.from_sync_json(payload)
        assert receipt is not None
        self.assertEqual(receipt.observation_id, "")
        self.assertFalse(receipt.has_valid_observation())
        self.assertFalse(receipt.is_target_policy)

    def test_invalid_payloads_return_none(self) -> None:
        for raw in (None, [], {}, {"context_id": ""}, {"context_id": "   "}, {"context_id": 123}):
            with self.subTest(raw=raw):
                self.assertIsNone(CoopReceipt.from_sync_json(raw))

    def test_bool_tab_id_is_ignored(self) -> None:
        receipt = CoopReceipt.from_sync_json({"context_id": "ctx_1", "tab_id": True})
        assert receipt is not None
        self.assertIsNone(receipt.tab_id)


class TestCommandClassification(unittest.TestCase):
    """``classify_command_coop`` 对照 bsk 0.5.0 策略表的全量分类测试。"""

    def test_targeted_commands(self) -> None:
        cases = [
            ["click", "@e3", "--session", "mnaa", "--json"],
            ["click", "#page a:nth-child(2)", "--session", "mnaa", "--json"],
            ["fill", "@e2", "--value", "hi", "--session", "mnaa", "--json"],
            ["select", "@e4", "--value", "opt", "--session", "mnaa", "--json"],
            ["hover", "@e5", "--session", "mnaa", "--json"],
            ["scroll-to", "@e6", "--session", "mnaa", "--json"],
            ["focus", "@e7", "--session", "mnaa", "--json"],
            ["blur", "@e8", "--session", "mnaa", "--json"],
            ["press", "Enter", "--ref", "@e2", "--session", "mnaa", "--json"],
            ["press", "Enter", "--selector", "input", "--session", "mnaa", "--json"],
            ["wheel", "@e9", "--delta-y", "200", "--session", "mnaa", "--json"],
            ["wheel", "--ref", "@e9", "--delta-y", "200", "--session", "mnaa", "--json"],
            ["evaluate", "document.title", "--unverified", "--session", "mnaa"],
            ["evaluate", "document.title", "--expect", "[]", "--session", "mnaa"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(classify_command_coop(argv), COOP_MODE_TARGETED)

    def test_strict_commands(self) -> None:
        cases = [
            ["screenshot", "--out", "a.png", "--full-page", "--session", "mnaa", "--json"],
            ["evaluate", "document.title", "--session", "mnaa", "--json"],
            [
                "evaluate",
                "document.title",
                "--expect",
                "[]",
                "--observation",
                "obs_1",
                "--session",
                "mnaa",
            ],
            ["press", "Enter", "--session", "mnaa", "--json"],
            ["wheel", "--delta-y", "300", "--session", "mnaa", "--json"],
            [
                "click",
                "@e3",
                "--capture",
                "cap_1",
                "--image-x",
                "10",
                "--image-y",
                "20",
                "--session",
                "mnaa",
            ],
            ["navigate", "back", "--session", "mnaa", "--json"],
            ["navigate", "forward", "--session", "mnaa", "--json"],
            ["navigate-back", "--session", "mnaa", "--json"],
            ["navigate-forward", "--session", "mnaa", "--json"],
            ["reload", "--hard", "--session", "mnaa", "--json"],
            ["debug", "--tab-id", "12", "--rule", "{}", "rule_add", "--session", "mnaa"],
            ["debug", "rule_enable", "r1", "--session", "mnaa"],
            ["debug", "rule_disable", "r1", "--session", "mnaa"],
            ["debug", "rule_remove", "r1", "--session", "mnaa"],
            ["debug", "--tab-id", "12", "replay", "req1", "--session", "mnaa"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(classify_command_coop(argv), COOP_MODE_STRICT)

    def test_strict_bootstrap_commands(self) -> None:
        cases = [
            ["navigate", "https://example.com", "--session", "mnaa", "--json"],
            ["window", "resize", "--width", "1280", "--height", "800", "--session", "mnaa"],
            ["emulate", "--device", "iphone-14", "--session", "mnaa", "--json"],
            ["tab", "close", "12", "--session", "mnaa", "--json"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(classify_command_coop(argv), COOP_MODE_STRICT_BOOTSTRAP)

    def test_readonly_and_management_commands_are_none(self) -> None:
        cases = [
            [],
            ["observe", "--session", "mnaa", "--json"],
            ["snapshot", "--session", "mnaa", "--json"],
            ["get-html", "--session", "mnaa", "--json"],
            ["screenshot", "--out", "a.png", "--session", "mnaa", "--json"],
            ["screenshot", "--ref", "@e3", "--out", "a.png", "--session", "mnaa", "--json"],
            ["console", "--since", "0", "--session", "mnaa", "--json"],
            ["network", "--since", "0", "--session", "mnaa", "--json"],
            ["wait-for-navigation", "--timeout", "10s", "--session", "mnaa", "--json"],
            ["request-help", "--prompt", "login", "--session", "mnaa", "--json"],
            ["tab", "list", "--session", "mnaa", "--json"],
            ["tab", "create", "--url", "https://a.com", "--session", "mnaa", "--json"],
            ["tab", "select", "12", "--session", "mnaa", "--json"],
            ["tab", "borrow", "12", "--session", "mnaa", "--json"],
            ["tab", "return", "12", "--session", "mnaa", "--json"],
            ["debug", "--tab-id", "12", "requests", "--session", "mnaa", "--json"],
            ["debug", "performance", "--session", "mnaa", "--json"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(classify_command_coop(argv), COOP_MODE_NONE)


class TestCoopHelpers(unittest.TestCase):
    """辅助纯函数：tab 提取、ref 检测、失效判定、参数拼接、内嵌观察提取。"""

    def test_extract_target_tab_id(self) -> None:
        self.assertEqual(extract_target_tab_id(["click", "@e1", "--tab-id", "42"]), 42)
        self.assertEqual(extract_target_tab_id(["click", "@e1", "--tab-id=42"]), 42)
        self.assertEqual(extract_target_tab_id(["tab", "close", "42", "--session", "mnaa"]), 42)
        self.assertIsNone(extract_target_tab_id(["click", "@e1", "--session", "mnaa"]))
        self.assertIsNone(extract_target_tab_id(["click", "@e1", "--tab-id", "bad"]))

    def test_has_snapshot_ref_target(self) -> None:
        self.assertTrue(has_snapshot_ref_target(["click", "@e3", "--session", "mnaa"]))
        self.assertTrue(has_snapshot_ref_target(["click", "e3", "--session", "mnaa"]))
        self.assertTrue(has_snapshot_ref_target(["fill", "@e12", "--value", "x"]))
        self.assertTrue(has_snapshot_ref_target(["press", "Enter", "--ref", "@e5"]))
        self.assertFalse(has_snapshot_ref_target(["click", "#page a:nth-child(2)"]))
        self.assertFalse(has_snapshot_ref_target(["click", ".e3"]))
        self.assertFalse(has_snapshot_ref_target(["wheel", "--delta-y", "200"]))

    def test_tab_and_blank_state_helpers(self) -> None:
        self.assertTrue(command_switches_default_tab(["tab", "create", "--url", "https://a.com"]))
        self.assertFalse(
            command_switches_default_tab(["tab", "create", "--url", "https://a.com", "--no-active"])
        )
        self.assertTrue(command_switches_default_tab(["tab", "select", "12"]))
        self.assertTrue(command_switches_default_tab(["tab", "borrow", "12"]))
        self.assertTrue(command_switches_default_tab(["tab", "return", "12"]))
        self.assertFalse(command_switches_default_tab(["tab", "list"]))
        self.assertFalse(command_switches_default_tab(["tab", "close", "12"]))

        self.assertTrue(command_leaves_blank_tab(["navigate", "https://a.com"]))
        self.assertTrue(command_leaves_blank_tab(["tab", "create", "--url", "https://a.com"]))
        self.assertFalse(
            command_leaves_blank_tab(["tab", "create", "--url", "https://a.com", "--no-active"])
        )
        self.assertTrue(command_invalidates_tab_context(["reload"]))
        self.assertTrue(command_invalidates_window_context(["window", "resize", "--width", "800"]))
        self.assertTrue(is_readonly_strict_command(["screenshot", "--out", "a.png", "--full-page"]))
        self.assertFalse(is_readonly_strict_command(["screenshot", "--out", "a.png"]))
        self.assertFalse(is_readonly_strict_command(["evaluate", "1+1"]))

    def test_attach_coop_flags(self) -> None:
        cap_v050 = CooperationCapability.from_help_text(_COOP_HELP_V050)
        cap_v040 = CooperationCapability.from_help_text(_COOP_HELP_V040)
        receipt = CoopReceipt(
            context_id="ctx_1",
            observation_id="obs_1",
            control_policy="target",
        )

        # targeted：只加 --context，不加 --observation，不修改原列表
        orig = ["click", "@e1", "--session", "mnaa", "--json"]
        out_targeted = attach_coop_flags(
            orig, receipt, mode=COOP_MODE_TARGETED, capability=cap_v050
        )
        self.assertEqual(orig, ["click", "@e1", "--session", "mnaa", "--json"])
        self.assertIn("--context", out_targeted)
        self.assertIn("ctx_1", out_targeted)
        self.assertNotIn("--observation", out_targeted)

        # strict (0.5.0)：同时加 --context 与 --observation
        out_strict = attach_coop_flags(
            ["screenshot", "--full-page", "--session", "mnaa"],
            receipt,
            mode=COOP_MODE_STRICT,
            capability=cap_v050,
        )
        self.assertIn("--context", out_strict)
        self.assertIn("--observation", out_strict)
        self.assertIn("obs_1", out_strict)

        # strict (0.4.0)：不支持 --observation 时只加 --context
        out_v040 = attach_coop_flags(
            ["screenshot", "--full-page", "--session", "mnaa"],
            receipt,
            mode=COOP_MODE_STRICT,
            capability=cap_v040,
        )
        self.assertIn("--context", out_v040)
        self.assertNotIn("--observation", out_v040)

        # 已存在时不重复拼接
        out_dup = attach_coop_flags(
            ["evaluate", "1+1", "--context", "custom_ctx", "--observation", "custom_obs"],
            receipt,
            mode=COOP_MODE_STRICT,
            capability=cap_v050,
        )
        self.assertEqual(out_dup.count("--context"), 1)
        self.assertEqual(out_dup.count("--observation"), 1)

    def test_extract_sync_observation(self) -> None:
        raw = {
            "kind": "baseline",
            "tab_id": 77,
            "observation": {"text": "RootWebArea \"Test\"", "ref_count": 3},
        }
        extracted = extract_sync_observation(raw)
        assert extracted is not None
        self.assertEqual(extracted["tab_id"], 77)
        self.assertNotIn("tab_id", raw["observation"])  # 未原地修改

        self.assertIsNone(extract_sync_observation({"kind": "unchanged", "context_id": "ctx_1"}))
        self.assertIsNone(extract_sync_observation({"kind": "delta", "changes": []}))


# ----------------------------------------------------------------------
# 2. bsk/errors.py 与 bsk/runner.py 错误细分单测
# ----------------------------------------------------------------------


class TestCooperationErrors(unittest.TestCase):
    """bsk 0.4.x / 0.5.0 新增错误码与 reason 分类契约。"""

    def test_permission_denied_legacy_reasons_preserve_window_scope_message(self) -> None:
        for reason in ("", "agent_window_scope"):
            with self.subTest(reason=reason):
                err = errors.classify(
                    exit_code=EXIT_USER_ERROR,
                    code=CODE_PERMISSION_DENIED,
                    message="agent window only",
                    reason=reason,
                )
                self.assertIn("只允许在 bsk 自己的窗口里执行", err.friendly)

    def test_permission_denied_cooperation_reasons_never_misreport_window_scope(self) -> None:
        reasons = (
            "cooperation_sync_required",
            "control_context_invalid",
            "observation_required",
            "observation_changed",
            "document_changed",
            "human_intervention",
            "control_held",
            "control_authority_required",
            "uncertain_prior_effect",
            "frame_unobservable",
            "page_load_failed",
            "element_not_visible",
            "target_hidden",
            "target_detached",
            "target_unavailable",
            "target_disabled",
            "target_readonly",
            "target_covered",
            "target_moved",
            "target_focus_lost",
            "virtual_row_recycled",
            "precondition_failed",
            "restricted_tab_url",
            "user_denied",
            "confirmation_timeout",
            "confirmation_ui_unavailable",
            "file_drop_target_unavailable",
            "local_file_error",
            "user_moved_tab",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                err = errors.classify(
                    exit_code=EXIT_USER_ERROR,
                    code=CODE_PERMISSION_DENIED,
                    message=f"rejected: {reason}",
                    reason=reason,
                )
                self.assertNotIn("只允许在 bsk 自己的窗口里执行", err.friendly)
                self.assertFalse(err.retryable)

    def test_permission_denied_unknown_future_reason_shows_reason_and_detail(self) -> None:
        err = errors.classify(
            exit_code=EXIT_USER_ERROR,
            code=CODE_PERMISSION_DENIED,
            message="some new bsk 0.6 policy rule",
            reason="future_guard_rule",
        )
        self.assertIn("future_guard_rule", err.friendly)
        self.assertIn("some new bsk 0.6 policy rule", err.friendly)
        self.assertNotIn("只允许在 bsk 自己的窗口里执行", err.friendly)

    def test_user_aborted_user_moved_tab_and_dialog_pending_not_session_gone(self) -> None:
        aborted = errors.classify(
            exit_code=EXIT_USER_ERROR,
            code=CODE_USER_ABORTED,
            message="stopped by user",
        )
        self.assertIs(type(aborted), BskError)
        self.assertFalse(aborted.retryable)
        self.assertIn("停止", aborted.friendly)

        moved = errors.classify(
            exit_code=EXIT_USER_ERROR,
            code=CODE_NOT_FOUND,
            reason="user_moved_tab",
            message="tab moved out of window",
        )
        self.assertNotIsInstance(moved, BskSessionGone)
        self.assertIn("移出", moved.friendly)

        for exc in (
            errors.classify(exit_code=EXIT_DIALOG_PENDING, code=CODE_DIALOG_PENDING, message="alert"),
            errors.classify(exit_code=EXIT_DIALOG_PENDING, code="", message="alert"),
        ):
            self.assertIs(type(exc), BskError)
            self.assertFalse(exc.retryable)
            self.assertIn("对话框", exc.friendly)

    def test_target_not_found_reasons_do_not_become_session_gone(self) -> None:
        for reason in ("ref_not_found", "selector_not_found", "visual_target_changed"):
            with self.subTest(reason=reason):
                exc = errors.classify(
                    exit_code=EXIT_USER_ERROR,
                    code=CODE_NOT_FOUND,
                    reason=reason,
                    message="element missing",
                )
                self.assertNotIsInstance(exc, BskSessionGone)
                self.assertFalse(exc.retryable)

        # 空 reason 的 not_found 仍然按会话失效处理（向后兼容）
        gone = errors.classify(
            exit_code=EXIT_USER_ERROR,
            code=CODE_NOT_FOUND,
            message="session not registered",
        )
        self.assertIsInstance(gone, BskSessionGone)

    def test_effect_state_unknown_and_new_outcome_reasons(self) -> None:
        exc = errors.classify(
            exit_code=EXIT_USER_ERROR,
            code=CODE_PERMISSION_DENIED,
            reason="human_intervention",
            message="interrupted mid-click",
            effect_state="unknown",
        )
        self.assertIsInstance(exc, BskOutcomeUnknown)
        self.assertFalse(exc.retryable)

        for reason in ("transfer_outcome_unknown", "borrow_outcome_unknown"):
            with self.subTest(reason=reason):
                err = errors.classify(
                    exit_code=EXIT_BROWSER,
                    code="cdp_failed",
                    reason=reason,
                    message="outcome unknown",
                )
                self.assertIsInstance(err, BskOutcomeUnknown)

    def test_browser_timeout_protocol_reason_hints(self) -> None:
        b1 = errors.classify(exit_code=EXIT_BROWSER, reason="operation_settling", message="settling")
        self.assertIsInstance(b1, BskBrowserError)
        self.assertIn("收尾", b1.friendly)

        b2 = errors.classify(exit_code=EXIT_BROWSER, reason="screenshot_capture_failed", message="changed")
        self.assertIn("视口截图", b2.friendly)

        b3 = errors.classify(exit_code=EXIT_BROWSER, reason="page_hidden", message="hidden")
        self.assertIn("隐藏", b3.friendly)

        t1 = errors.classify(exit_code=EXIT_TIMEOUT, reason="loading_stalled", message="stalled")
        self.assertIsInstance(t1, BskTimeout)
        self.assertIn("加载状态", t1.friendly)

        p1 = errors.classify(exit_code=EXIT_PROTOCOL, reason="user_cancelled", message="cancelled")
        self.assertIsInstance(p1, BskProtocolError)
        self.assertIn("用户中断", p1.friendly)

    def test_runner_extract_effect_state_and_4tuple_compat(self) -> None:
        flat = {
            "code": "permission_denied",
            "message": "interrupted",
            "hint": "check page",
            "data": {"reason": "human_intervention", "effect_state": "UNKNOWN"},
        }
        self.assertEqual(
            _extract_error_fields(flat),
            ("permission_denied", "interrupted", "check page", "human_intervention"),
        )
        self.assertEqual(_extract_effect_state(flat), "unknown")

        nested = {"error": {"code": "cdp_failed", "data": {"effect_state": "none"}}}
        self.assertEqual(_extract_effect_state(nested), "none")
        self.assertEqual(_extract_effect_state({}), "")
        self.assertEqual(_extract_effect_state(None), "")


# ----------------------------------------------------------------------
# 3. 端到端模拟器：精确模拟 bsk 0.5.0 / 0.4.0 / 0.3.3 协作状态机
# ----------------------------------------------------------------------


def _arg_value(args: list[str], flag: str) -> str:
    for idx, tok in enumerate(args):
        if tok == flag and idx + 1 < len(args):
            return args[idx + 1]
    return ""


class SimulatedBskRunner:
    """高保真模拟 bsk CLI 的协作控制校验规则。

    支持：
    - ``version="0.5.0"``：完整 ``target`` 策略，区分 ``--context`` 与顶层 ``--observation``
      （绝不接受嵌套 ``observation.observation_id``），严格校验空白页引导与凭证消费；
    - ``version="0.4.0"``：仅 ``strict`` 策略（无 ``--observation`` / ``--policy``），每次动作消耗 context；
    - ``version="0.3.3"``：无 ``cooperate`` 子命令，拒绝任何 ``--context`` / ``--observation``。
    """

    def __init__(self, *, version: str = "0.5.0", old_extension: bool = False) -> None:
        self.version = version
        self.old_extension = old_extension
        self.calls: list[list[str]] = []
        self._session_seq = 0
        self._ctx_seq = 0
        self._obs_seq = 0

        # 可注入的一次性/持久故障钩子
        self.next_sync_responses: list[Any] = []
        self.next_cmd_errors: dict[str, list[BskError]] = {}

        # 每个会话的状态
        self.sessions: dict[str, dict[str, Any]] = {}

    def resolve(self) -> str:
        return "fake-bsk"

    def _new_session(self) -> dict[str, Any]:
        self._session_seq += 1
        sid = f"u{chr(96 + self._session_seq)}en"
        state = {
            "session_id": sid,
            "default_tab": 10,
            "tabs": {
                10: {
                    "url": "about:blank",
                    "bootstrap": True,
                    "title": "",
                    "policy": "",
                    "context_id": "",
                    "observation_id": "",
                    "obs_used": False,
                    "ctx_used": False,
                    "synced_once": False,
                }
            },
        }
        self.sessions[sid] = state
        return state

    def _get_tab(self, sid: str, tab_id: int | None) -> tuple[int, dict[str, Any]]:
        sess = self.sessions.get(sid)
        if sess is None:
            raise errors.classify(
                exit_code=EXIT_USER_ERROR,
                code=CODE_NOT_FOUND,
                message=f"session {sid} not found",
            )
        tid = tab_id if tab_id is not None else sess["default_tab"]
        if tid not in sess["tabs"]:
            sess["tabs"][tid] = {
                "url": "https://example.com/tab",
                "bootstrap": False,
                "title": f"Tab {tid}",
                "policy": "",
                "context_id": "",
                "observation_id": "",
                "obs_used": False,
                "ctx_used": False,
                "synced_once": False,
            }
        return tid, sess["tabs"][tid]

    async def run_or_raise(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        expect_json: bool = True,
    ) -> BskResult:
        del timeout, expect_json
        self.calls.append(list(args))
        cmd = args[0] if args else ""

        # --- 能力探测 ---
        if args[:2] == ["cooperate", "--help"]:
            if self.version == "0.3.3":
                raise errors.classify(
                    exit_code=EXIT_USER_ERROR,
                    stderr="error: unrecognized subcommand 'cooperate'",
                )
            text = _COOP_HELP_V050 if self.version == "0.5.0" else _COOP_HELP_V040
            return BskResult(ok=True, exit_code=0, stdout=text)

        if args[:2] == ["session", "request"] and "--help" in args:
            # 简化测试，跳过 request-id 协议往返
            raise errors.classify(exit_code=EXIT_USER_ERROR, stderr="no session request")

        # --- session start / stop ---
        if args[:2] == ["session", "start"]:
            sess = self._new_session()
            return BskResult(
                ok=True,
                exit_code=0,
                data={
                    "session_id": sess["session_id"],
                    "browser_instance_id": "edge-1",
                    "agent_window_id": 100 + self._session_seq,
                    "interaction": "agent_window",
                },
            )

        if args[:2] == ["session", "stop"]:
            self.sessions.pop(args[2], None)
            return BskResult(ok=True, exit_code=0, data={"ok": True})

        # --- 通用注入错误 ---
        injected = self.next_cmd_errors.get(cmd)
        if injected:
            raise injected.pop(0)

        # --- 0.3.3 严格拒绝新标志 ---
        if self.version == "0.3.3":
            if "--context" in args or "--observation" in args or cmd == "cooperate":
                raise errors.classify(
                    exit_code=EXIT_USER_ERROR,
                    stderr=f"error: unexpected argument in bsk 0.3.3: {args}",
                )

        sid = _arg_value(args, "--session")
        raw_tab = _arg_value(args, "--tab-id")
        tab_id = int(raw_tab) if raw_tab else None

        # --- cooperate sync ---
        if args[:2] == ["cooperate", "sync"]:
            if self.next_sync_responses:
                nxt = self.next_sync_responses.pop(0)
                if isinstance(nxt, BaseException):
                    raise nxt
                return BskResult(ok=True, exit_code=0, data=nxt)

            tid, tab = self._get_tab(sid, tab_id)
            req_policy = _arg_value(args, "--policy") or "auto"
            if self.old_extension and req_policy != "strict":
                raise errors.classify(
                    exit_code=EXIT_USER_ERROR,
                    code=CODE_UNSUPPORTED,
                    reason="unsupported_feature",
                    message="extension protocol < 1.8 does not support target policy",
                )
            if tab["url"] == "about:blank":
                raise errors.classify(
                    exit_code=EXIT_USER_ERROR,
                    code=CODE_PERMISSION_DENIED,
                    reason="frame_unobservable",
                    message="cannot observe about:blank",
                )

            effective_policy = (
                "strict"
                if (self.version == "0.4.0" or req_policy == "strict")
                else "target"
            )
            self._ctx_seq += 1
            self._obs_seq += 1
            ctx_id = f"ctx_{self._ctx_seq}"
            obs_id = f"obs_{self._obs_seq}"
            kind = "unchanged" if tab["synced_once"] else "baseline"

            tab["policy"] = effective_policy
            tab["context_id"] = ctx_id
            tab["observation_id"] = obs_id
            tab["ctx_used"] = False
            tab["obs_used"] = False
            tab["synced_once"] = True
            tab["bootstrap"] = False

            payload: dict[str, Any] = {
                "kind": kind,
                "ready": True,
                "tab_id": tid,
                "context_id": ctx_id,
                "control_policy": effective_policy,
            }
            if self.version == "0.5.0":
                payload["observation_id"] = obs_id
            if kind == "baseline":
                payload["observation"] = {
                    "observation_id": f"nested_ref_obs_{self._obs_seq}",
                    "tab_id": tid,
                    "text": (
                        f'RootWebArea "{tab["title"] or "Example"}"\n'
                        '  link "Next" [ref=@e1]\n'
                        '  textbox "Query" [ref=@e2]\n'
                        '  canvas "Chart" [ref=@e3]'
                    ),
                    "ref_count": 3,
                }
            return BskResult(ok=True, exit_code=0, data=payload)

        # --- 校验受协作保护的命令（bsk >= 0.4.0） ---
        mode = classify_command_coop(args)
        ctx_arg = _arg_value(args, "--context")
        obs_arg = _arg_value(args, "--observation")

        if cmd == "tab" and len(args) >= 3 and args[1] == "close":
            tab_id = int(args[2])

        tid, tab = self._get_tab(sid, tab_id)

        if self.version != "0.3.3" and mode != COOP_MODE_NONE:
            if not (mode == COOP_MODE_STRICT_BOOTSTRAP and tab["bootstrap"]):
                if not ctx_arg:
                    raise errors.classify(
                        exit_code=EXIT_USER_ERROR,
                        code=CODE_PERMISSION_DENIED,
                        reason="cooperation_sync_required",
                        message="missing --context",
                    )
                if ctx_arg != tab["context_id"] or tab["ctx_used"]:
                    raise errors.classify(
                        exit_code=EXIT_USER_ERROR,
                        code=CODE_PERMISSION_DENIED,
                        reason="control_context_invalid",
                        message=f"stale context {ctx_arg}",
                    )
                need_obs = (
                    self.version == "0.5.0"
                    and tab["policy"] == "target"
                    and mode in (COOP_MODE_STRICT, COOP_MODE_STRICT_BOOTSTRAP)
                )
                if need_obs:
                    if not obs_arg:
                        raise errors.classify(
                            exit_code=EXIT_USER_ERROR,
                            code=CODE_PERMISSION_DENIED,
                            reason="observation_required",
                            message="This action requires an unused strict observation from cooperate sync",
                        )
                    if obs_arg.startswith("nested_ref_obs_"):
                        raise errors.classify(
                            exit_code=EXIT_USER_ERROR,
                            code=CODE_PERMISSION_DENIED,
                            reason="observation_required",
                            message="nested observation.observation_id is not a valid strict observation",
                        )
                    if obs_arg != tab["observation_id"] or tab["obs_used"]:
                        raise errors.classify(
                            exit_code=EXIT_USER_ERROR,
                            code=CODE_PERMISSION_DENIED,
                            reason="observation_changed",
                            message="strict observation already consumed or stale",
                        )
                    tab["obs_used"] = True
                if tab["policy"] == "strict":
                    tab["ctx_used"] = True

        # --- 执行各业务命令 ---
        if cmd == "navigate":
            target = args[1]
            if target in ("back", "forward"):
                tab["url"] = f"https://example.com/{target}"
            else:
                tab["url"] = target
                tab["title"] = f"Page {target}"
            tab["bootstrap"] = False
            tab["synced_once"] = False
            tab["context_id"] = ""
            tab["observation_id"] = ""
            return BskResult(
                ok=True,
                exit_code=0,
                data={"url": tab["url"], "final_url": tab["url"], "reached": "load"},
            )

        if cmd == "observe":
            if tab["policy"] == "strict":
                tab["ctx_used"] = True
            return BskResult(
                ok=True,
                exit_code=0,
                data={
                    "tab_id": tid,
                    "text": (
                        f'RootWebArea "{tab["title"] or "Blank"}"\n'
                        '  link "Next" [ref=@e1]\n'
                        '  textbox "Query" [ref=@e2]'
                    ),
                    "ref_count": 2,
                },
            )

        if cmd == "snapshot":
            if tab["policy"] == "strict":
                tab["ctx_used"] = True
            return BskResult(
                ok=True,
                exit_code=0,
                data={"tab_id": tid, "text": 'RootWebArea "Snap"\n  button "Go" [ref=@e1]', "ref_count": 1},
            )

        if cmd in ("click", "fill", "select", "hover", "scroll-to", "focus", "blur", "press", "wheel"):
            return BskResult(ok=True, exit_code=0, data={"ok": True, "tab_id": tid})

        if cmd == "evaluate":
            return BskResult(ok=True, exit_code=0, data={"ok": True, "value": "Example Title"})

        if cmd == "screenshot":
            out_path = _arg_value(args, "--out")
            if out_path:
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_bytes(_MINIMAL_PNG)
            return BskResult(
                ok=True,
                exit_code=0,
                data={
                    "path": out_path,
                    "width": 1280,
                    "height": 4200 if "--full-page" in args else 720,
                    "byte_size": len(_MINIMAL_PNG),
                    "capture_id": "cap_1" if "--ref" in args else "",
                },
            )

        if cmd == "tab":
            sub = args[1]
            sess = self.sessions[sid]
            if sub == "create":
                new_tid = max(sess["tabs"]) + 10
                url = _arg_value(args, "--url") or "about:blank"
                sess["tabs"][new_tid] = {
                    "url": url,
                    "bootstrap": url == "about:blank",
                    "title": "" if url == "about:blank" else f"Page {url}",
                    "policy": "",
                    "context_id": "",
                    "observation_id": "",
                    "obs_used": False,
                    "ctx_used": False,
                    "synced_once": False,
                }
                if "--no-active" not in args:
                    sess["default_tab"] = new_tid
                return BskResult(ok=True, exit_code=0, data={"id": new_tid, "tab_id": new_tid})
            if sub == "select":
                sess["default_tab"] = int(args[2])
                return BskResult(ok=True, exit_code=0, data={"tab_id": int(args[2])})
            if sub == "close":
                sess["tabs"].pop(int(args[2]), None)
                return BskResult(ok=True, exit_code=0, data={"closed": True})
            return BskResult(ok=True, exit_code=0, data=[])

        if cmd == "window" and len(args) >= 2 and args[1] == "resize":
            sess = self.sessions[sid]
            for t in sess["tabs"].values():
                t["context_id"] = ""
                t["observation_id"] = ""
            return BskResult(ok=True, exit_code=0, data={"width": 1024, "height": 768})

        if cmd == "emulate":
            return BskResult(ok=True, exit_code=0, data={"device": _arg_value(args, "--device")})

        if cmd in ("console", "network"):
            return BskResult(ok=True, exit_code=0, data={"entries": []})

        return BskResult(ok=True, exit_code=0, data={"ok": True})


def _make_service(
    runner: SimulatedBskRunner, tmpdir: str
) -> tuple[BskService, SessionManager]:
    settings = parse_settings(
        {
            "bsk_path": "fake-bsk",
            "screenshot_dir": tmpdir,
            "enable_request_id": False,
            "command_timeout_sec": 30.0,
        }
    )
    sessions = SessionManager(
        runner,  # type: ignore[arg-type]
        settings,
        stop_max_attempts=1,
        stop_retry_delay_sec=0.0,
        stop_attempt_overhead_sec=0.0,
    )
    service = BskService(settings, runner=runner, sessions=sessions)  # type: ignore[arg-type]
    return service, sessions


class TestCooperationEndToEnd(unittest.IsolatedAsyncioTestCase):
    """覆盖 Issue #7 与所有协作控制边界的端到端集成测试。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_issue7_full_flow_on_bsk_v050(self) -> None:
        """Issue #7 完整场景：启动 -> observe -> CSS click -> ref fill -> 连续两次全页截图。"""
        runner = SimulatedBskRunner(version="0.5.0")
        service, sessions = _make_service(runner, self._tmp.name)

        # 1. 启动并导航到目标页（初始空白页走 bootstrap，不调用 cooperate sync）
        started = await service.start_session(url="https://search.example.com", key="qq:g1")
        sid = started["session_id"]
        sync_calls = [c for c in runner.calls if c[:2] == ["cooperate", "sync"]]
        self.assertEqual(len(sync_calls), 0)

        # 2. 用 4 字母 session_id 调 observe：归并回原会话，1 次 sync baseline 直接复用 VOM
        obs = await service.observe(sid)
        self.assertIn("Next", obs.text)
        self.assertEqual(len(runner.sessions), 1)  # 未重复新建空白会话
        self.assertEqual(len([c for c in runner.calls if c[0] == "observe"]), 0)
        self.assertEqual(len([c for c in runner.calls if c[:2] == ["cooperate", "sync"]]), 1)

        # 3. Issue #7 场景 1：点击 CSS 选择器 -> 带 --context，不带 --observation，不重复 sync
        await service.interact(sid, "click", target="#page a:nth-child(2)")
        click_call = next(c for c in runner.calls if c[0] == "click")
        self.assertEqual(_arg_value(click_call, "--context"), "ctx_1")
        self.assertEqual(_arg_value(click_call, "--observation"), "")

        # 4. 连续第二次 DOM 交互：复用同一个 --context，零额外 sync
        await service.interact(sid, "fill", target="@e2", value="astrbot")
        fill_call = next(c for c in runner.calls if c[0] == "fill")
        self.assertEqual(_arg_value(fill_call, "--context"), "ctx_1")
        self.assertEqual(len([c for c in runner.calls if c[:2] == ["cooperate", "sync"]]), 1)

        # 5. Issue #7 场景 2：第一次全页截图 -> 带 --context + 顶层 --observation（绝非 nested）
        shot1 = await service.screenshot_ex(sid, full_page=True)
        self.assertTrue(os.path.exists(shot1.path))
        shot_calls = [c for c in runner.calls if c[0] == "screenshot"]
        self.assertEqual(_arg_value(shot_calls[0], "--context"), "ctx_1")
        self.assertEqual(_arg_value(shot_calls[0], "--observation"), "obs_1")

        # 6. 第二次全页截图：旧 observation 已消耗，自动重同步获取 obs_2 后成功
        shot2 = await service.screenshot_ex(sid, full_page=True)
        self.assertTrue(os.path.exists(shot2.path))
        shot_calls = [c for c in runner.calls if c[0] == "screenshot"]
        self.assertEqual(_arg_value(shot_calls[1], "--observation"), "obs_2")
        self.assertEqual(sessions.stats()["counters"]["coop_syncs"], 2)

        await sessions.close()

    async def test_direct_click_and_fullpage_screenshot_without_prior_observe(self) -> None:
        """跳过 observe 直接点击或全页截图时，SessionManager 会自动补做 cooperate sync。"""
        runner = SimulatedBskRunner(version="0.5.0")
        service, sessions = _make_service(runner, self._tmp.name)

        started = await service.start_session(url="https://example.com", key="qq:g2")
        sid = started["session_id"]

        # 直接 CSS 点击
        await service.interact(sid, "click", target="#page a:nth-child(2)")
        click_call = next(c for c in runner.calls if c[0] == "click")
        self.assertEqual(_arg_value(click_call, "--context"), "ctx_1")

        # 直接全页截图（复用刚同步的未消耗 obs_1）
        shot = await service.screenshot(sid, full_page=True)
        self.assertTrue(os.path.exists(shot.path))
        shot_call = next(c for c in runner.calls if c[0] == "screenshot")
        self.assertEqual(_arg_value(shot_call, "--observation"), "obs_1")

        await sessions.close()

    async def test_fullpage_screenshot_retries_once_on_stale_observation(self) -> None:
        """全页截图是只读严格命令：遇到 observation_changed 时自动重同步并重试一次。"""
        runner = SimulatedBskRunner(version="0.5.0")
        service, sessions = _make_service(runner, self._tmp.name)

        started = await service.start_session(url="https://example.com", key="qq:g3")
        sid = started["session_id"]
        await service.observe(sid)

        runner.next_cmd_errors["screenshot"] = [
            errors.classify(
                exit_code=EXIT_USER_ERROR,
                code=CODE_PERMISSION_DENIED,
                reason="observation_changed",
                message="page mutated before full-page capture",
            )
        ]

        shot = await service.screenshot_ex(sid, full_page=True)
        self.assertTrue(os.path.exists(shot.path))
        self.assertEqual(sessions.stats()["counters"]["coop_retries"], 1)
        shot_calls = [c for c in runner.calls if c[0] == "screenshot"]
        self.assertEqual(len(shot_calls), 2)
        self.assertEqual(_arg_value(shot_calls[0], "--observation"), "obs_1")
        self.assertEqual(_arg_value(shot_calls[1], "--observation"), "obs_2")

        await sessions.close()

    async def test_fullpage_screenshot_never_retries_on_human_or_unknown_effect(self) -> None:
        """人工接管、用户取消或 effect_state=unknown 时，连全页截图也绝不自动重试。"""
        for injected in (
            errors.classify(
                exit_code=EXIT_USER_ERROR,
                code=CODE_PERMISSION_DENIED,
                reason="human_intervention",
                message="user touched tab",
            ),
            errors.classify(
                exit_code=EXIT_PROTOCOL,
                reason="user_cancelled",
                message="user scrolled during capture",
            ),
            errors.classify(
                exit_code=EXIT_USER_ERROR,
                code=CODE_PERMISSION_DENIED,
                reason="observation_changed",
                message="unknown state",
                effect_state="unknown",
            ),
        ):
            with self.subTest(reason=injected.reason, cls=type(injected).__name__):
                runner = SimulatedBskRunner(version="0.5.0")
                service, sessions = _make_service(runner, self._tmp.name)
                started = await service.start_session(url="https://example.com", key="qq:g4")
                await service.observe(started["session_id"])

                runner.next_cmd_errors["screenshot"] = [injected]
                with self.assertRaises(BskError):
                    await service.screenshot_ex(started["session_id"], full_page=True)

                self.assertEqual(sessions.stats()["counters"]["coop_retries"], 0)
                self.assertEqual(len([c for c in runner.calls if c[0] == "screenshot"]), 1)
                await sessions.close()

    async def test_evaluate_strict_observation_and_no_auto_retry(self) -> None:
        """evaluate 必须附带 --context 与 --observation，且失败绝不自动重试。"""
        runner = SimulatedBskRunner(version="0.5.0")
        service, sessions = _make_service(runner, self._tmp.name)
        started = await service.start_session(url="https://example.com", key="qq:g5")
        sid = started["session_id"]
        await service.observe(sid)

        ev = await service.evaluate(sid, "document.title")
        self.assertEqual(ev.value, "Example Title")
        eval_call = next(c for c in runner.calls if c[0] == "evaluate")
        self.assertEqual(_arg_value(eval_call, "--context"), "ctx_1")
        self.assertEqual(_arg_value(eval_call, "--observation"), "obs_1")

        # 遇到 observation_changed 绝不重试
        runner.next_cmd_errors["evaluate"] = [
            errors.classify(
                exit_code=EXIT_USER_ERROR,
                code=CODE_PERMISSION_DENIED,
                reason="observation_changed",
                message="dom changed",
            )
        ]
        with self.assertRaises(BskError) as ctx:
            await service.evaluate(sid, "1+1")
        self.assertEqual(ctx.exception.reason, "observation_changed")
        self.assertEqual(sessions.stats()["counters"]["coop_retries"], 0)

        # 遇到 effect_state=unknown 标记会话为 uncertain 并拒绝后续写操作
        runner.next_cmd_errors["evaluate"] = [
            errors.classify(
                exit_code=EXIT_BROWSER,
                code="cdp_failed",
                message="disconnected mid-eval",
                effect_state="unknown",
            )
        ]
        with self.assertRaises(BskOutcomeUnknown):
            await service.evaluate(sid, "submitForm()")
        with self.assertRaises(BskOutcomeUnknown):
            await service.interact(sid, "click", target="#btn")

        await sessions.close()

    async def test_stale_ref_guard_blocks_click_after_document_baseline_change(self) -> None:
        """自动重同步返回全新 baseline 时，禁止拿旧 @eN 盲点（但允许 unchanged 与 CSS 选择器）。"""
        runner = SimulatedBskRunner(version="0.5.0")
        service, sessions = _make_service(runner, self._tmp.name)
        started = await service.start_session(url="https://example.com/a", key="qq:g6")
        sid = started["session_id"]
        await service.observe(sid)

        # 导航到新页面（凭证清空，但 had_sync 仍为 True）
        await service.navigate(sid, "https://example.com/b")

        # 模型忘记 observe，直接点击旧 @e1 -> 自动 sync 返回 baseline -> 安全防线拦截
        with self.assertRaises(BskError) as ctx:
            await service.interact(sid, "click", target="@e1")
        self.assertEqual(ctx.exception.reason, "document_changed")
        self.assertEqual(len([c for c in runner.calls if c[0] == "click"]), 0)

        # 重新 observe 后再点 @e1 -> 放行
        await service.observe(sid)
        await service.interact(sid, "click", target="@e1")
        self.assertEqual(len([c for c in runner.calls if c[0] == "click"]), 1)

        await sessions.close()

    async def test_human_control_and_user_aborted_safety(self) -> None:
        """人工接管时只读 observe 降级并提示模型；用户停止时直接中止不重试。"""
        runner = SimulatedBskRunner(version="0.5.0")
        service, sessions = _make_service(runner, self._tmp.name)
        started = await service.start_session(url="https://example.com", key="qq:g7")
        sid = started["session_id"]

        # 人工接管（sync 返回 ready=False, state.mode="human"，无 context_id）
        runner.next_sync_responses.append(
            {"ready": False, "state": {"mode": "human"}, "tab_id": 10}
        )
        obs = await service.observe(sid)
        self.assertIn("用户当前正在操作该标签页", obs.text)
        self.assertIn("Next", obs.text)

        # 用户主动停止（stopped: true）-> 报 user_aborted，绝不降级成正常观察
        runner.next_sync_responses.append({"stopped": True, "ready": False})
        with self.assertRaises(BskError) as ctx:
            await service.observe(sid)
        self.assertEqual(ctx.exception.code, CODE_USER_ABORTED)
        self.assertIn("停止", ctx.exception.friendly)

        await sessions.close()

    async def test_observe_on_blank_tab_after_rebuild_falls_back_gracefully(self) -> None:
        """会话被回收重建后回到 about:blank，observe 遇到 frame_unobservable 时降级并保留重建提示。"""
        runner = SimulatedBskRunner(version="0.5.0")
        service, sessions = _make_service(runner, self._tmp.name)
        started = await service.start_session(url="https://example.com", key="qq:g8")
        sid = started["session_id"]
        await service.observe(sid)

        # 模拟 daemon 侧已空闲回收该会话
        runner.sessions.clear()

        obs = await service.observe(sid)
        self.assertIn("自动重建", obs.text)
        self.assertEqual(sessions.stats()["counters"]["not_found_rebuilds"], 1)

        await sessions.close()

    async def test_legacy_bsk_v033_sends_zero_cooperation_commands_or_flags(self) -> None:
        """老版本 bsk <= 0.3.3 完全不发 cooperate sync，也不拼接 --context / --observation。"""
        runner = SimulatedBskRunner(version="0.3.3")
        service, sessions = _make_service(runner, self._tmp.name)

        started = await service.start_session(url="https://example.com", key="qq:legacy")
        sid = started["session_id"]
        await service.observe(sid)
        await service.interact(sid, "click", target="@e1")
        shot = await service.screenshot_ex(sid, full_page=True)
        self.assertTrue(os.path.exists(shot.path))
        await service.evaluate(sid, "document.title")

        self.assertEqual(
            len([c for c in runner.calls if c[:2] == ["cooperate", "sync"]]),
            0,
        )
        for call in runner.calls:
            self.assertNotIn("--context", call)
            self.assertNotIn("--observation", call)

        await sessions.close()

    async def test_bsk_v040_strict_policy_without_observation_flag(self) -> None:
        """bsk 0.4.0 无 --observation/--policy：只传 --context 且每次动作后重新同步。"""
        runner = SimulatedBskRunner(version="0.4.0")
        service, sessions = _make_service(runner, self._tmp.name)

        started = await service.start_session(url="https://example.com", key="qq:v040")
        sid = started["session_id"]
        await service.observe(sid)

        await service.interact(sid, "click", target="@e1")
        await service.interact(sid, "fill", target="@e2", value="hi")
        await service.screenshot_ex(sid, full_page=True)

        for call in runner.calls:
            self.assertNotIn("--observation", call)
            self.assertNotIn("--policy", call)
        # strict 策略下每次动作消耗凭证，共 3 次同步（observe 1 次 + fill 自动 1 次 + screenshot 自动 1 次）
        self.assertEqual(sessions.stats()["counters"]["coop_syncs"], 3)

        await sessions.close()

    async def test_v050_cli_with_old_extension_falls_back_to_strict_policy(self) -> None:
        """新版 CLI 搭配旧扩展（协议 < 1.8）报 unsupported_feature 时自动降级 --policy strict。"""
        runner = SimulatedBskRunner(version="0.5.0", old_extension=True)
        service, sessions = _make_service(runner, self._tmp.name)

        started = await service.start_session(url="https://example.com", key="qq:mixed")
        sid = started["session_id"]
        await service.observe(sid)
        await service.interact(sid, "click", target="#btn")
        await service.interact(sid, "fill", target="#input", value="ok")

        sync_calls = [c for c in runner.calls if c[:2] == ["cooperate", "sync"]]
        # 第 1 次不带 --policy（失败），第 2 次降级 --policy strict（成功），
        # 第 3 次（fill 前）直接带 --policy strict（不再重复试 auto）
        self.assertEqual(len(sync_calls), 3)
        self.assertNotIn("--policy", sync_calls[0])
        self.assertEqual(_arg_value(sync_calls[1], "--policy"), "strict")
        self.assertEqual(_arg_value(sync_calls[2], "--policy"), "strict")

        await sessions.close()

    async def test_multitab_window_resize_and_blank_tab_navigation(self) -> None:
        """多标签页凭证隔离、tab close 同步、新建空白页导航以及 window resize 失效全窗凭证。"""
        runner = SimulatedBskRunner(version="0.5.0")
        service, sessions = _make_service(runner, self._tmp.name)

        started = await service.start_session(url="https://example.com/t1", key="qq:mt")
        sid = started["session_id"]
        await service.observe(sid)  # 默认 tab 10 -> ctx_1

        # 新建带 URL 的标签页 20（成为默认页）
        created = await service.tabs(sid, "create", url="https://example.com/t2")
        self.assertEqual(created["id"], 20)
        await service.observe(sid, tab_id=20)  # tab 20 -> ctx_2

        # 分别向 tab 10 和 tab 20 发送点击，各自带上对应 tab 的 --context
        await service.interact(sid, "click", target="#a", tab_id=10)
        await service.interact(sid, "click", target="#b", tab_id=20)
        clicks = [c for c in runner.calls if c[0] == "click"]
        self.assertEqual(_arg_value(clicks[0], "--context"), "ctx_1")
        self.assertEqual(_arg_value(clicks[1], "--context"), "ctx_2")

        # window resize（非空白页）附带严格观察并清空所有标签页凭证
        await service.resize_window(sid, 1024, 768)
        resize_call = next(c for c in runner.calls if c[:2] == ["window", "resize"])
        self.assertTrue(_arg_value(resize_call, "--context"))
        self.assertTrue(_arg_value(resize_call, "--observation"))
        self.assertFalse(sessions.has_valid_coop_context("qq:mt", 10))
        self.assertFalse(sessions.has_valid_coop_context("qq:mt", 20))

        # tab close 20 自动同步 tab 20 后关闭
        await service.tabs(sid, "close", tab_id=20)
        close_call = next(c for c in runner.calls if c[:2] == ["tab", "close"])
        self.assertTrue(_arg_value(close_call, "--context"))
        self.assertTrue(_arg_value(close_call, "--observation"))

        # 新建无 URL 空白页 30，随后 navigate：sync 报 frame_unobservable 时放行引导导航
        created_blank = await service.tabs(sid, "create")
        self.assertEqual(created_blank["id"], 30)
        nav = await service.navigate(sid, "https://example.com/t3")
        self.assertEqual(nav["final_url"], "https://example.com/t3")

        await sessions.close()

    async def test_canvas_visual_click_and_snapshot_bootstrap(self) -> None:
        """Canvas 视觉点击复用 observe 留下的未使用 observation；snapshot 首次触发同步启用稳定 ref。"""
        runner = SimulatedBskRunner(version="0.5.0")
        service, sessions = _make_service(runner, self._tmp.name)

        started = await service.start_session(url="https://example.com/canvas", key="qq:cv")
        sid = started["session_id"]

        # snapshot 在尚无有效上下文时先 sync 一次，第二次 snapshot 不再重复 sync
        await service.snapshot(sid)
        await service.snapshot(sid)
        self.assertEqual(sessions.stats()["counters"]["coop_syncs"], 1)

        # 元素裁剪截图（不带 --full-page）不消耗 observation_id
        shot = await service.screenshot_ex(sid, ref="@e3")
        self.assertTrue(os.path.exists(shot.path))
        shot_call = next(c for c in runner.calls if c[0] == "screenshot")
        self.assertNotIn("--observation", shot_call)

        # Canvas 视觉点击（--capture）是 strict 模式，复用未消耗的 obs_1 无需额外 sync
        await service.interact(
            sid,
            "click",
            target="@e3",
            capture_id="cap_1",
            image_x=120,
            image_y=80,
        )
        click_call = next(c for c in runner.calls if c[0] == "click")
        self.assertEqual(_arg_value(click_call, "--context"), "ctx_1")
        self.assertEqual(_arg_value(click_call, "--observation"), "obs_1")
        self.assertEqual(sessions.stats()["counters"]["coop_syncs"], 1)

        await sessions.close()

    async def test_session_id_alias_resolves_across_legacy_service_methods(self) -> None:
        """向 observe / evaluate / screenshot / read_console / read_network 传 session_id 不会建重复会话。"""
        runner = SimulatedBskRunner(version="0.5.0")
        service, sessions = _make_service(runner, self._tmp.name)

        started = await service.start_session(url="https://example.com", key="qq:alias")
        sid = started["session_id"]

        await service.observe(sid)
        await service.evaluate(sid, "1+1")
        await service.screenshot(sid, full_page=False)
        await service.read_console(sid)
        await service.read_network(sid)

        self.assertEqual(len(runner.sessions), 1)
        self.assertEqual(sessions.stats()["sessions"], 1)

        await sessions.close()


if __name__ == "__main__":
    unittest.main()
