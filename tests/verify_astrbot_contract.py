"""L2 契约测试：在真实 AstrBot 环境里加载插件，验证工具注册。

这不是语法检查 —— 它让 AstrBot 自己解析 docstring 生成工具 schema。
框架打印出 ``Added llm tool: bsk_*`` 才算通过。

用法（必须让 ``data.plugins`` 可被 import，模拟 AstrBot 的真实加载路径）：
    $env:PYTHONPATH = "D:\\AstrBot\\backend\\app;C:\\Users\\<你>\\.astrbot;<本目录>"
    python tests/verify_astrbot_contract.py

注意：AstrBot 用 ``__import__("data.plugins.<插件目录>.main")`` 加载插件，
所以 ``~/.astrbot`` 必须在 ``sys.path`` 上，且插件要真的装在
``~/.astrbot/data/plugins/`` 下（Python 命名空间包会处理缺失的 ``__init__.py``）。

前置条件：插件已安装到 ``~/.astrbot/data/plugins/astrbot_plugin_bsk_browser/``。
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback

FAILURES: list[str] = []
CHECKS: list[tuple[str, bool, str]] = []


def _ensure_paths() -> None:
    """把必要的路径放进 sys.path，让本脚本可以直接运行。

    - AstrBot 应用目录：import astrbot
    - ~/.astrbot：import data.plugins.<插件>
    - 本测试目录：import astrbot_test_doubles

    另外必须把 ``ASTRBOT_ROOT`` 钉到用户真实目录：AstrBot 解析数据路径时
    优先读它，否则普通模式下会用当前工作目录（core/utils/astrbot_path.py:29-35）。
    不设置的话，在项目目录里运行会在项目内生成 ``data/cmd_config.json``
    （AstrBot 主配置，含 API 密钥与管理员 QQ 号）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    project = os.path.dirname(here)
    for path in (here, project):
        if path not in sys.path:
            sys.path.insert(0, path)

    astrbot_app = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
    if os.path.isdir(astrbot_app) and astrbot_app not in sys.path:
        sys.path.insert(0, astrbot_app)

    # 先"设置"再"读取"：只在没设过时才写入，避免覆盖用户显式配置。
    astrbot_root = os.environ.get(
        "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
    )
    os.environ.setdefault("ASTRBOT_ROOT", astrbot_root)
    if os.path.isdir(astrbot_root) and astrbot_root not in sys.path:
        sys.path.insert(0, astrbot_root)


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def main() -> int:
    _ensure_paths()

    print("=" * 72)
    print("L2 契约测试：真实 AstrBot 环境加载")
    print("=" * 72)

    # --- 1. 基础环境 ---
    try:
        import astrbot

        ver = getattr(astrbot, "__version__", "unknown")
        check("import astrbot", True, f"版本 {ver}")
        print(f"[ok]   astrbot 版本: {ver}")
    except Exception:
        check("import astrbot", False, traceback.format_exc())
        print("[fail] 无法 import astrbot —— PYTHONPATH 是否包含 D:\\AstrBot\\backend\\app？")
        return 1

    # --- 2. 按 AstrBot 的真实方式 import 插件 ---
    # AstrBot 用 __import__(path, fromlist=[module_str])，
    # path = "data.plugins.<dir>.<module>"，所以 CWD 必须是 ~/.astrbot。
    module_name = "data.plugins.astrbot_plugin_bsk_browser.main"
    try:
        module = __import__(module_name, fromlist=["main"])
        check("import 插件模块", True, module_name)
        print(f"[ok]   import {module_name}")
    except Exception:
        check("import 插件模块", False, traceback.format_exc())
        print("[fail] 插件 import 失败：")
        traceback.print_exc()
        return 1

    # --- 3. 插件类与硬约束（C1-C4）---
    try:
        from astrbot.api.star import Star

        cls = module.BskBrowserPlugin
        check("插件类存在", True, cls.__name__)
        check("继承 Star", issubclass(cls, Star), str(cls.__mro__[:3]))

        # C3：绝不定义 __del__（否则 terminate 永不执行）
        check(
            "未定义 __del__（C3）",
            "__del__" not in cls.__dict__,
            "定义了 __del__ 会让 terminate() 永不执行！",
        )
        # C4：钩子必须定义在本类上
        check("terminate 定义在本类（C4）", "terminate" in cls.__dict__, "")
        check("initialize 定义在本类（C4）", "initialize" in cls.__dict__, "")

        # C7：config 必须有默认值
        import inspect

        sig = inspect.signature(cls.__init__)
        cfg = sig.parameters.get("config")
        check(
            "config 有默认值（C7）",
            cfg is not None and cfg.default is not inspect.Parameter.empty,
            f"签名: {sig}",
        )
        print(f"[ok]   硬约束检查通过（无 __del__、钩子在本类、config 有默认值）")
    except Exception:
        check("插件类检查", False, traceback.format_exc())
        traceback.print_exc()

    # --- 4. 工具是否被框架注册（这是核心）---
    try:
        from astrbot.core.provider.register import llm_tools

        names = {t.name for t in llm_tools.func_list}
        expected = {
            "bsk_open",
            "bsk_read",
            "bsk_act",
            "bsk_screenshot",
            "bsk_close",
            "bsk_status",
        }
        missing = expected - names
        found = expected & names

        check("6 个工具全部注册", not missing, f"缺少: {sorted(missing)}")
        print(f"[ok]   已注册工具: {sorted(found)}")
        if missing:
            print(f"[FAIL] 缺少工具: {sorted(missing)}")
            print(f"       当前全部工具: {sorted(names)}")

        # --- 5. 每个工具的 schema 非空且参数类型合法（C6）---
        for tool in llm_tools.func_list:
            if tool.name not in expected:
                continue
            params = getattr(tool, "parameters", {}) or {}
            props = params.get("properties", {}) if isinstance(params, dict) else {}
            desc = (getattr(tool, "description", "") or "").strip()
            check(f"{tool.name} 有描述", bool(desc), "docstring 描述为空会导致模型不知道何时调用")
            print(f"      - {tool.name}: 参数 {list(props)} / 描述 {desc[:40]}...")
            for pname, pspec in props.items():
                ptype = pspec.get("type")
                ok = ptype in {"string", "number", "object", "array", "boolean"}
                check(f"{tool.name}.{pname} 类型合法", ok, f"type={ptype}")
    except Exception:
        check("工具注册检查", False, traceback.format_exc())
        traceback.print_exc()

    # --- 6. 实例化并跑生命周期（不碰浏览器）---
    async def lifecycle() -> None:
        try:
            from astrbot_test_doubles import FakeEvent, make_context

            cls = module.BskBrowserPlugin
            ctx = make_context()
            inst = cls(ctx, config={"enabled": True, "admin_only": True})
            check("实例化插件", True, type(inst).__name__)
            print("[ok]   插件实例化成功")

            await inst.initialize()
            check("initialize() 可调用", True, "")
            print("[ok]   initialize() 正常返回")

            # 权限：非管理员应被拒绝
            denied_text = inst._denied(FakeEvent(is_admin=False))
            check("非管理员被拒绝", denied_text is not None, "权限门失效！")
            allowed_text = inst._denied(FakeEvent(is_admin=True))
            check("管理员被放行", allowed_text is None, f"管理员也被拒: {allowed_text}")

            # 会话键：umo 模式下一个群共用一个 key
            key_group = inst._key(FakeEvent(umo="aiocqhttp:group:1", sender_id="a"))
            key_group2 = inst._key(FakeEvent(umo="aiocqhttp:group:1", sender_id="b"))
            check("umo 模式下同群同键", key_group == key_group2, f"{key_group} != {key_group2}")

            key_other = inst._key(FakeEvent(umo="aiocqhttp:group:2", sender_id="a"))
            check("不同群不同键", key_group != key_other, f"{key_group} == {key_other}")

            # 关闭（因为没有真实会话，应当安静地返回 0）
            await inst.terminate()
            check("terminate() 可调用且不抛异常", True, "")
            print("[ok]   terminate() 正常返回（无会话可关）")
        except Exception:
            check("生命周期", False, traceback.format_exc())
            traceback.print_exc()

    asyncio.run(lifecycle())

    # --- 汇总 ---
    print()
    print("=" * 72)
    total = len(CHECKS)
    failed = len(FAILURES)
    print(f"检查项：{total}，失败：{failed}")
    if FAILURES:
        print()
        for f in FAILURES:
            print(f"  [FAIL] {f}")
        print()
        print("结果：FAILED")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
