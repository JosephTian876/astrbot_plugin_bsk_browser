"""全链路集成验证：页面交互弹性（auto-scroll）、会话键解析与错误诊断呈现。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))
if str(_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tests"))

from astrbot_test_doubles import ensure_astrbot_modules, make_context, FakeEvent

ensure_astrbot_modules()

from astrbot_plugin_bsk_browser.main import BskBrowserPlugin
from astrbot_plugin_bsk_browser.bsk.errors import classify
from astrbot_plugin_bsk_browser.bsk.models import BskResult
from astrbot_plugin_bsk_browser.bsk.service import BskService
from astrbot_plugin_bsk_browser.bsk.config import parse_settings


class FakeTestRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.handlers: dict[str, Any] = {}

    async def run_or_raise(
        self, args: list[str], *, timeout: float | None = None, expect_json: bool = True
    ) -> BskResult:
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "session":
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "start":
                return BskResult(
                    ok=True,
                    exit_code=0,
                    data={"session_id": "sid-101", "browser_instance_id": "b-101"},
                )
            return BskResult(ok=True, exit_code=0, data={})

        if cmd in self.handlers:
            res = await self.handlers[cmd](args)
            if not res.ok:
                from astrbot_plugin_bsk_browser.bsk.runner import _extract_error_fields

                code, message, hint, reason = _extract_error_fields(res.data)
                if not reason and isinstance(res.data, dict):
                    reason = str(res.data.get("reason") or "")
                if not message and isinstance(res.data, dict):
                    message = str(res.data.get("message") or "")
                if not code and isinstance(res.data, dict):
                    code = str(res.data.get("code") or "permission_denied")
                raise classify(
                    exit_code=res.exit_code,
                    code=code,
                    message=message,
                    hint=hint,
                    reason=reason,
                    stderr=res.stderr,
                )
            return res

        return BskResult(ok=True, exit_code=0, data={})


class TestInteractionResilienceSuite(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runner = FakeTestRunner()
        settings = parse_settings(
            {
                "bsk_path": "bsk",
                "browser_instance_id": "b-101",
                "command_timeout_sec": 30.0,
                "screenshot_dir": self.tmp.name,
                "enable_request_id": False,
            }
        )
        self.service = BskService(settings, runner=self.runner)
        self.plugin = BskBrowserPlugin(make_context(), config={"admin_only": False})
        self.plugin.service = self.service
        self.event = FakeEvent(is_admin=True, umo="test:user:1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_e2e_element_not_visible_auto_scroll_click_flow(self) -> None:
        """测试元素初次点击不在视口内，自动触发 scroll-to 并在视口内重试点击成功。"""
        session_info = await self.service.start_session(key="test:user:1")
        session_id = session_info["session_id"]
        self.runner.calls.clear()

        click_count = 0

        async def handle_click(args: list[str]) -> BskResult:
            nonlocal click_count
            click_count += 1
            if click_count == 1:
                return BskResult(
                    ok=False,
                    exit_code=1,
                    data={
                        "code": "permission_denied",
                        "reason": "element_not_visible",
                        "message": "Element not visible in viewport",
                    },
                )
            return BskResult(
                ok=True,
                exit_code=0,
                data={"session": session_id, "target": "@e27", "clicked": True},
            )

        async def handle_scroll_to(args: list[str]) -> BskResult:
            return BskResult(
                ok=True,
                exit_code=0,
                data={"session": session_id, "target": "@e27", "scrolled": True},
            )

        self.runner.handlers["click"] = handle_click
        self.runner.handlers["scroll-to"] = handle_scroll_to

        res = await self.plugin._dispatch(
            "bsk_interact", self.event, {"action": "click", "target": "@e27"}
        )

        cmds = [c[0] for c in self.runner.calls]
        self.assertEqual(cmds, ["click", "scroll-to", "click"])
        self.assertIn("@e27", self.runner.calls[1])
        self.assertIn("@e27", self.runner.calls[2])

        self.assertIn("已执行 click。", res)
        self.assertIn("目标元素：@e27", res)
        self.assertIn(
            "（注意：目标元素原先位于视口外，已自动将其滚动至视口中心并完成操作。）", res
        )
        self.assertNotIn("auto_scrolled", res)

    async def test_e2e_observe_with_session_id_keeps_session(self) -> None:
        """测试使用 session_id 调用 observe 时，不会因为 key 混淆导致重建会话或创建幽灵 entry。"""
        session_info = await self.service.start_session(key="test:user:1")
        session_id = session_info["session_id"]
        self.runner.calls.clear()

        async def handle_observe(args: list[str]) -> BskResult:
            return BskResult(
                ok=True,
                exit_code=0,
                data={
                    "text": "@vom 1\n@view 910x604\nL1 page\n  RootWebArea \"测试页面\"\n    @e1 button \"提交\"",
                    "ref_count": 1,
                    "tab_id": 1,
                },
            )

        self.runner.handlers["observe"] = handle_observe

        res = await self.plugin._dispatch(
            "bsk_inspect", self.event, {"action": "observe", "session": session_id}
        )

        self.assertIn("测试页面", res)
        self.assertIn("@e1", res)
        self.assertIn("test:user:1", self.service.sessions._entries)
        self.assertNotIn(session_id, self.service.sessions._entries)

    async def test_e2e_true_agent_window_scope_error(self) -> None:
        """测试真实的窗口越界错误被准确分类并附带技术诊断。"""
        await self.service.start_session(key="test:user:1")
        self.runner.calls.clear()

        async def handle_click(args: list[str]) -> BskResult:
            return BskResult(
                ok=False,
                exit_code=1,
                data={
                    "code": "permission_denied",
                    "reason": "agent_window_scope",
                    "message": "action refused: tab belongs to user",
                },
            )

        self.runner.handlers["click"] = handle_click

        res = await self.plugin._dispatch(
            "bsk_interact", self.event, {"action": "click", "target": "@e1"}
        )
        self.assertIn("该动作只允许在 bsk 自己的窗口里执行", res)
        self.assertIn("（原因: agent_window_scope", res)
        self.assertIn("详情: action refused: tab belongs to user", res)

    async def test_e2e_control_authority_required_error(self) -> None:
        """测试控制权被占用时错误准确分类并附带技术诊断。"""
        await self.service.start_session(key="test:user:1")
        self.runner.calls.clear()

        async def handle_click(args: list[str]) -> BskResult:
            return BskResult(
                ok=False,
                exit_code=1,
                data={
                    "code": "permission_denied",
                    "reason": "control_authority_required",
                    "message": "another client is controlling the browser",
                },
            )

        self.runner.handlers["click"] = handle_click

        res = await self.plugin._dispatch(
            "bsk_interact", self.event, {"action": "click", "target": "@e1"}
        )
        self.assertIn("当前页面需要用户控制权或外部确认", res)
        self.assertIn("（原因: control_authority_required", res)

    async def test_e2e_element_not_visible_failure_when_persistent(self) -> None:
        """测试自动滚动后依然不可见时，抛出准确的 element_not_visible 错误并附带诊断。"""
        await self.service.start_session(key="test:user:1")
        self.runner.calls.clear()

        async def handle_click(args: list[str]) -> BskResult:
            return BskResult(
                ok=False,
                exit_code=1,
                data={
                    "code": "permission_denied",
                    "reason": "element_not_visible",
                    "message": "element hidden behind modal",
                },
            )

        async def handle_scroll_to(args: list[str]) -> BskResult:
            return BskResult(ok=True, exit_code=0, data={"scrolled": True})

        self.runner.handlers["click"] = handle_click
        self.runner.handlers["scroll-to"] = handle_scroll_to

        res = await self.plugin._dispatch(
            "bsk_interact", self.event, {"action": "click", "target": "@e99"}
        )
        self.assertIn("目标元素当前不在视口可视范围内", res)
        self.assertIn("（原因: element_not_visible", res)
        cmds = [c[0] for c in self.runner.calls]
        self.assertEqual(cmds, ["click", "scroll-to", "click"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
