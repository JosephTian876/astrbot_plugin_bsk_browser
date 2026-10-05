"""``bsk_evaluate`` 权限门的专项验证 —— 三条分支，不需要真实浏览器。

## 为什么单独一个脚本

``bsk evaluate`` 能在用户已登录的页面里执行任意 JavaScript，是本插件风险
最高的能力。它的权限门有三条分支，其中第 2 条是最容易写错、也最要命的一条：

    即使把 ``admin_only`` 关掉（或把调用者加进 ``allowed_users`` 白名单），
    只要 ``evaluate_require_admin`` 为 True，非管理员依然不能执行脚本。

这条规则的存在理由：``admin_only=False`` 的语义是"我愿意把看得见的浏览器
操作开放给其他人"，而执行脚本是静默的（能读页面数据、能带 cookie 发请求）。
让一个粗粒度的宽松开关顺手把最高危能力一起放开，是最难察觉的权限放大路径。

把这个验证并进 ``tests/verify_tools_e2e.py`` 不合适：那个脚本需要真实浏览器、
并且用 SHA256 严格比对安装目录（本脚本只测源码目录，改完立刻能跑）。
所以这里新建一个，且完全不碰浏览器：用假 event + 假 service。

## 三条分支（外加几条边界）

| # | enabled | enable_evaluate | evaluate_require_admin | admin_only | 调用者 | 期望 |
|---|---|---|---|---|---|---|
| 1 | true | false | — | true | 管理员 | 拒绝（默认路径）|
| 2 | true | true | true | false | 非管理员 | 仍拒绝 |
| 2b| true | true | true | false | 白名单里的非管理员 | 仍拒绝 |
| 3 | true | true | true | true | 管理员 | 放行 |
| 3b| true | true | false | false | 非管理员 | 放行（需显式关两项）|
| 4 | false | true | — | — | 管理员 | 拒绝（总开关优先）|

用法：
    python tests/verify_evaluate_gate.py

退出码：全部通过 0，有任何失败 1。

安全边界：本脚本从不执行任何 JavaScript，也不创建任何 bsk 会话 ——
它注入的假 service 只记录"被调用了"，不会走到子进程。
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    """记录一条用例并立即打印。"""
    RESULTS.append((name, ok, detail))
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if detail else ""))
    return ok


def _ensure_paths() -> None:
    """把必要路径放进 ``sys.path``。

    - 本目录：``import astrbot_test_doubles``
    - 仓库根：``import bsk``
    - 仓库根的上一级：``import astrbot_plugin_bsk_browser.main``
      （目录名本身就是包名，Python 的命名空间包会处理缺失的 ``__init__.py``）
    - AstrBot 应用目录：``import astrbot``

    必须先把 ``ASTRBOT_ROOT`` 钉到用户真实目录：AstrBot 解析数据路径时优先
    读它，否则普通模式下会用当前工作目录（``astrbot_path.py:29-35``），
    在项目里运行时会在仓库内生成 ``data/cmd_config.json``（AstrBot 主配置，
    含 API 密钥与管理员 QQ 号）—— 而本仓库是要公开发布的。
    """
    for path in (str(HERE), str(PROJECT), str(PROJECT.parent)):
        if path not in sys.path:
            sys.path.insert(0, path)

    astrbot_app = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
    if os.path.isdir(astrbot_app) and astrbot_app not in sys.path:
        sys.path.insert(0, astrbot_app)

    astrbot_root = os.environ.get(
        "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
    )
    os.environ.setdefault("ASTRBOT_ROOT", astrbot_root)
    if os.path.isdir(astrbot_root) and astrbot_root not in sys.path:
        sys.path.insert(0, astrbot_root)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeService:
    """假的 ``BskService``：只记录调用，绝不碰子进程/浏览器。

    把它注入插件实例，就能回答"这次调用到底有没有被放行" —— 因为放行的唯一
    表现就是 ``service.evaluate`` 被调用了。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.raise_error: BaseException | None = None
        self.value: object = "Example Domain"

    async def evaluate(self, key: str, expression: str):
        """冒充一次脚本执行，返回可渲染的假结果。"""
        from astrbot_plugin_bsk_browser.bsk.models import EvaluateResult

        self.calls.append((key, expression))
        if self.raise_error is not None:
            raise self.raise_error
        return EvaluateResult(ok=True, value=self.value, has_value=True)

    def render_evaluate(self, result, expression: str) -> str:
        """用真实渲染逻辑才需要；这里给一个可识别的标记即可。"""
        return f"RENDERED[{expression}]={result.value!r}"


def make_plugin(config: dict):
    """构造一个插件实例，并把 ``service`` 换成假实现。

    Returns:
        ``(插件实例, 假 service)``。
    """
    from astrbot_plugin_bsk_browser.main import BskBrowserPlugin
    from astrbot_test_doubles import make_context

    # 显式指定浏览器，避免任何探测路径；假 service 也不会用到它。
    full = {"browser_instance_id": "c900a3da", **config}
    plugin = BskBrowserPlugin(make_context(), config=full)
    fake = FakeService()
    plugin.service = fake
    return plugin, fake


async def call_tool(plugin, event, expression: str) -> str:
    """await ``bsk_evaluate`` 并返回文本结果（异常转成字符串供断言）。"""
    try:
        result = await plugin.bsk_evaluate(event, expression=expression)
        return str(result)
    except BaseException as exc:  # noqa: BLE001 - 工具函数绝不该抛，这里兜住以便报错
        return f"<RAISED {type(exc).__name__}: {exc}>"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def run() -> int:
    from astrbot_test_doubles import FakeEvent

    admin = FakeEvent(is_admin=True, sender_id="1", umo="gate:umo:admin")
    nonadmin = FakeEvent(is_admin=False, sender_id="10001", umo="gate:umo:user")

    print("=" * 72)
    print("bsk_evaluate 权限门验证（三分支，不执行任何 JavaScript）")
    print("=" * 72)

    # ---------------------------------------------------------------
    print("\n--- 分支 1：独立开关关闭（默认）→ 拒绝 ---")
    # ---------------------------------------------------------------
    try:
        plugin, fake = make_plugin({"enabled": True, "enable_evaluate": False})
        # 连管理员也被拒 —— 证明这道门是"能力开关"，不是"身份检查"。
        text = await call_tool(plugin, admin, "document.title")

        record(
            "1.1 enable_evaluate=false 时管理员也被拒绝",
            fake.calls == [],
            f"service 被调用 {len(fake.calls)} 次（应为 0）",
        )
        record(
            "1.2 拒绝文案说明如何在配置里开启",
            "enable_evaluate" in text and "默认关闭" in text,
            text[:60].replace("\n", " "),
        )
        record(
            "1.3 拒绝文案说明了风险（不只是「未启用」）",
            "已登录" in text and "任意脚本" in text,
            "",
        )
    except Exception:
        record("分支 1", False, traceback.format_exc())

    # ---------------------------------------------------------------
    print("\n--- 分支 2：开关开启 + 强制管理员，admin_only=false → 仍拒绝 ---")
    # ---------------------------------------------------------------
    try:
        plugin, fake = make_plugin(
            {
                "enabled": True,
                "enable_evaluate": True,
                "evaluate_require_admin": True,
                "admin_only": False,  # 总开关被关掉
            }
        )
        text = await call_tool(plugin, nonadmin, "document.title")

        record(
            "2.1 admin_only=false 也不能让非管理员执行脚本",
            fake.calls == [],
            f"service 被调用 {len(fake.calls)} 次（应为 0）",
        )
        record(
            "2.2 文案说明该限制不受 admin_only 影响",
            "不受" in text and "仅限 AstrBot 管理员" in text,
            text[:50].replace("\n", " "),
        )
        record(
            "2.3 文案里带上调用者 ID，便于管理员定位",
            "10001" in text,
            "",
        )

        # 同一个实例下，管理员必须仍然能用 —— 证明不是"谁都拒绝了"。
        admin_text = await call_tool(plugin, admin, "document.title")
        record(
            "2.4 同一配置下管理员仍被放行（证明门没写死）",
            len(fake.calls) == 1 and "RENDERED" in admin_text,
            f"管理员调用后 service 次数={len(fake.calls)}",
        )
    except Exception:
        record("分支 2", False, traceback.format_exc())

    # ---------------------------------------------------------------
    print("\n--- 分支 2b：白名单里的非管理员 → 也不能绕过 ---")
    # ---------------------------------------------------------------
    try:
        plugin, fake = make_plugin(
            {
                "enabled": True,
                "enable_evaluate": True,
                "evaluate_require_admin": True,
                "admin_only": True,
                # 白名单优先于 admin_only，这是别的工具的既有语义；
                #   对 evaluate 它必须失效。
                "allowed_users": ["10001"],
            }
        )

        # 先验证前提：白名单确实让这个非管理员通过了普通权限门
        # （否则这条用例就没在测"盖过白名单"）。用 bsk_read 的门来对照。
        ordinary_denied = plugin._denied(nonadmin)
        record(
            "2b.0 前提：白名单让该非管理员通过普通权限门（_denied 返回 None）",
            ordinary_denied is None,
            f"_denied={ordinary_denied!r}",
        )

        text = await call_tool(plugin, nonadmin, "document.title")
        record(
            "2b.1 白名单不能绕过 evaluate 的管理员要求",
            fake.calls == [],
            f"service 被调用 {len(fake.calls)} 次（应为 0）",
        )
        record(
            "2b.2 拒绝文案提到了白名单不生效",
            "白名单" in text,
            text[:50].replace("\n", " "),
        )
    except Exception:
        record("分支 2b", False, traceback.format_exc())

    # ---------------------------------------------------------------
    print("\n--- 分支 3：管理员 + 开关打开 → 放行 ---")
    # ---------------------------------------------------------------
    try:
        plugin, fake = make_plugin(
            {
                "enabled": True,
                "enable_evaluate": True,
                "evaluate_require_admin": True,
                "admin_only": True,
            }
        )
        text = await call_tool(plugin, admin, "document.title")

        record(
            "3.1 管理员 + 开关打开 → service 被调用一次",
            len(fake.calls) == 1,
            f"calls={fake.calls}",
        )
        record(
            "3.2 表达式被原样传给服务层",
            fake.calls and fake.calls[0][1] == "document.title",
            f"calls={fake.calls}",
        )
        record(
            "3.3 结果是渲染后的文本（不是裸对象）",
            "RENDERED[document.title]" in text,
            text[:60],
        )
        record(
            "3.4 会话键按 umo 计算",
            fake.calls and fake.calls[0][0] == "gate:umo:admin",
            f"key={fake.calls[0][0] if fake.calls else None}",
        )
    except Exception:
        record("分支 3", False, traceback.format_exc())

    # ---------------------------------------------------------------
    print("\n--- 分支 3b：显式关掉 evaluate_require_admin → 非管理员可用 ---")
    # ---------------------------------------------------------------
    try:
        plugin, fake = make_plugin(
            {
                "enabled": True,
                "enable_evaluate": True,
                "evaluate_require_admin": False,  # 显式关掉（需两次决定）
                "admin_only": False,
            }
        )
        text = await call_tool(plugin, nonadmin, "1+1")

        record(
            "3b.1 两项都显式关掉后非管理员才能用",
            len(fake.calls) == 1,
            f"calls={fake.calls}",
        )
        record(
            "3b.2 仍然返回渲染结果",
            "RENDERED[1+1]" in text,
            text[:60],
        )
    except Exception:
        record("分支 3b", False, traceback.format_exc())

    # ---------------------------------------------------------------
    print("\n--- 分支 4：插件总开关优先 ---")
    # ---------------------------------------------------------------
    try:
        plugin, fake = make_plugin(
            {
                "enabled": False,  # 总开关关闭
                "enable_evaluate": True,
                "evaluate_require_admin": False,
                "admin_only": False,
            }
        )
        text = await call_tool(plugin, admin, "document.title")

        record(
            "4.1 enabled=false 时管理员也被拒绝",
            fake.calls == [],
            f"service 被调用 {len(fake.calls)} 次（应为 0）",
        )
        record(
            "4.2 文案指向总开关",
            "停用" in text,
            text[:50].replace("\n", " "),
        )
    except Exception:
        record("分支 4", False, traceback.format_exc())

    # ---------------------------------------------------------------
    print("\n--- 边界：空表达式 / 服务层异常 ---")
    # ---------------------------------------------------------------
    try:
        plugin, fake = make_plugin(
            {"enabled": True, "enable_evaluate": True, "admin_only": True}
        )

        blank = await call_tool(plugin, admin, "   ")
        record(
            "5.1 空表达式被拒绝且不调用服务层",
            fake.calls == [] and "请提供" in blank,
            blank[:50],
        )

        from astrbot_plugin_bsk_browser.bsk.errors import BskError

        fake.raise_error = BskError("boom", friendly="脚本报错了：Error: boom")
        err_text = await call_tool(plugin, admin, "throw new Error('boom')")
        record(
            "5.2 服务层抛 BskError 时转成中文文本（不抛给框架）",
            "脚本报错了" in err_text and "RAISED" not in err_text,
            err_text[:60].replace("\n", " "),
        )

        fake.raise_error = RuntimeError("未预期")
        # 工具函数会 astrbot_logger.exception(...) 打一条带堆栈的日志 ——
        # 那是刻意的（真出问题时得能排查），但会让本脚本输出很难读，
        # 所以临时把该 logger 的级别提上去，只为让结果清爽。
        import logging

        plugin_logger = logging.getLogger("astrbot_plugin_bsk_browser.main")
        previous_level = plugin_logger.level
        plugin_logger.setLevel(logging.CRITICAL)
        try:
            unexpected = await call_tool(plugin, admin, "1+1")
        finally:
            plugin_logger.setLevel(previous_level)

        record(
            "5.3 未预期异常被吞掉并转成文本（绝不让堆栈进聊天）",
            "RAISED" not in unexpected and "未预期" in unexpected,
            unexpected[:60].replace("\n", " "),
        )
    except Exception:
        record("边界", False, traceback.format_exc())

    # ---------------------------------------------------------------
    print("\n--- 静态检查：evaluate 必须是独立工具，且没混进 bsk_act ---")
    # ---------------------------------------------------------------
    try:
        from astrbot_plugin_bsk_browser.bsk.service import BskService

        act_args = BskService._build_action_args(
            "click", target="@e1", value="", values=None, key_spec="",
            delta_y=0, delta_x=0,
        )
        record(
            "6.1 bsk_act 的动作参数里没有 evaluate",
            "evaluate" not in act_args,
            f"argv={act_args}",
        )

        # 不支持的动作必须报错（而不是悄悄当成 evaluate）。
        try:
            BskService._build_action_args(
                "evaluate", target="", value="", values=None, key_spec="",
                delta_y=0, delta_x=0,
            )
            record("6.2 bsk_act 不接受 evaluate 动作", False, "居然没报错")
        except BskError:
            record(
                "6.2 bsk_act 不接受 evaluate 动作",
                True,
                "抛 BskError（不支持的动作）",
            )

        # 独立工具必须真的注册成 bsk_evaluate。
        plugin, _ = make_plugin({"enabled": True})
        handler = getattr(plugin.bsk_evaluate, "__func__", plugin.bsk_evaluate)
        record(
            "6.3 bsk_evaluate 是插件自己的方法",
            callable(handler),
            "",
        )
    except Exception:
        record("静态检查", False, traceback.format_exc())

    # ---------------------------------------------------------------
    print()
    print("=" * 72)
    failed = [r for r in RESULTS if not r[1]]
    print(f"用例：{len(RESULTS)}，失败：{len(failed)}")
    for name, _, detail in failed:
        print(f"  [FAIL] {name}: {detail}")
    print("结果：" + ("全部通过" if not failed else "有失败"))
    return 1 if failed else 0


def main() -> int:
    _ensure_paths()
    # 输出里有中文：Windows 下 stdout 默认可能是 cp936，
    # 显式转 UTF-8，避免打印时抛 UnicodeEncodeError（见 ARCHITECTURE C9）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
