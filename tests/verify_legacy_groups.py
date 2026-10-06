"""验证旧工具的两级开关：``legacy_tools`` × ``legacy_fringe_tools`` 四种组合。

## 为什么单独一个脚本

``legacy_tools`` 从"全开或全关"改成了两级：常用的 4 个旧工具（core）常驻，
罕见的 3 个（fringe，都有等价新工具）挂在 ``legacy_fringe_tools`` 后面。
这一步改的是**注册期**的行为，用 ``astrbot.core`` 真实的工具注册表
（``llm_tools``）与真实的 ``initialize()`` 才测得准 —— 自己拼一份名单
等于把被测逻辑抄一遍，测不出接线错误。

## 四种组合的期望

| legacy_tools | legacy_fringe_tools | 注册数 | 说明 |
|---|---|---|---|
| true  | true  | 15 | 7 新 + 8 旧 |
| true  | false | 12 | 7 新 + 4 core（fringe 3 个停用）|
| false | true  | 8  | 7 新 + ``bsk_evaluate`` |
| false | false | 8  | 同上（fringe 开关无意义）|

（``bsk_debug`` 从 ``bsk_inspect`` 拆出后新工具是 7 个，上表已计入。）

另外验两条不变式：

1. ``bsk_evaluate`` **始终**不受这两个开关影响 —— 它只由 ``enable_evaluate``
   决定（``enable_evaluate=false`` 时它也在注册表里，只是调用会被权限门拒绝，
   因为那个开关是**运行时**的权限门，不是注册期开关）。
2. core 的 4 个在 ``legacy_tools=true`` 时始终注册，与 fringe 开关无关。

用法：
    & 'D:\\AstrBot\\backend\\python\\python.exe' tests/verify_legacy_groups.py

退出码：全部通过 0，有任何失败 1。
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
for path in (PROJECT, HERE, str(PROJECT.parent)):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_ASTRBOT_APP = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
_ASTRBOT_ROOT = os.environ.get("ASTRBOT_ROOT", str(Path.home() / ".astrbot"))
os.environ.setdefault("ASTRBOT_ROOT", _ASTRBOT_ROOT)

# ⚠️ 加载的是**源码目录**那份，不是 ``~/.astrbot/data/plugins/`` 下安装的那份。
#
# 这两份是独立的目录（实测：安装目录不是 junction/symlink），安装那份由用户
# 按需同步。本脚本测的是"我刚改的代码有没有正确接线"，所以必须走源码：
# 走安装目录会在旧代码上通过并给出假信心 —— 本轮就踩过这个坑：
# 安装那份还没有 ``legacy_fringe_tools``，于是组合 2 报 14 个（旧行为）。
#
# 走源码必须让 ``astrbot_plugin_bsk_browser`` 作为**包**被 import（目录名即包名，
# 靠命名空间包机制），所以把 ``PROJECT.parent`` 放进 sys.path（上面已做），
# 并且要用 ``astrbot_plugin_bsk_browser.main`` 这个模块名 —— 见 observe()。
#
# ``tests/verify_tools_e2e.py`` 走的是安装目录那条路，但它开头会用 SHA256
# 严格比对两份代码、不一致就失败。本脚本刻意不复制那套：这里只测注册期接线，
# 不碰浏览器，用源码跑更快也更直接。
for path in (_ASTRBOT_APP, _ASTRBOT_ROOT):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

# 源码树里的插件模块名（PROJECT 的父目录在 sys.path 上，所以它是可 import 的包）。
# 用 ``.main`` 这个入口时，main.py 里 ``from .bsk import ...`` 的相对 import 才成立。
SOURCE_MODULE = "astrbot_plugin_bsk_browser.main"

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if detail else ""))


# 7 个新工具。写死在测试里（不从 main 里 import NEW_TOOL_NAMES）是刻意的：
# 用被测代码的常量去算期望值，等于"两边一起错就测不出来"。
NEW_TOOLS: tuple[str, ...] = (
    "bsk_session",
    "bsk_page",
    "bsk_inspect",
    "bsk_debug",
    "bsk_interact",
    "bsk_tabs",
    "bsk_assist",
)

# core：常用且无等价替代的旧工具，``legacy_tools=true`` 时应当始终在。
CORE_TOOLS: tuple[str, ...] = (
    "bsk_screenshot",
    "bsk_close",
    "bsk_status",
    "bsk_logs",
)

# fringe：都有等价新工具，由 ``legacy_fringe_tools`` 控制。
FRINGE_TOOLS: tuple[str, ...] = (
    "bsk_open",
    "bsk_read",
    "bsk_act",
)


async def observe(config: dict) -> set[str]:
    """用给定配置跑一次真实 ``initialize()``，返回**处于 active 的** bsk_ 工具名。

    每次都要先把上一次留下的 bsk_ 工具从注册表里清掉，否则上一轮的
    ``active=False`` 会残留下来，把这一轮的结果污染成"都没注册"。
    """
    import importlib

    from astrbot.core.provider.register import llm_tools
    from astrbot_test_doubles import make_context

    for tool in list(llm_tools.func_list):
        if tool.name.startswith("bsk_"):
            llm_tools.remove_func(tool.name)

    module_name = SOURCE_MODULE
    sys.modules.pop(module_name, None)
    sys.modules.pop("astrbot_plugin_bsk_browser", None)
    module = importlib.import_module(module_name)

    plugin = module.BskBrowserPlugin(make_context(), config=config)
    await plugin.initialize()

    names = {
        tool.name
        for tool in llm_tools.func_list
        if tool.name.startswith("bsk_") and getattr(tool, "active", True)
    }
    await plugin.terminate()
    return names


def check_group(
    label: str, got: set[str], *, expect_legacy: bool, expect_fringe: bool
) -> None:
    """核对一种组合的注册结果：总数 + 逐个名字。"""
    expected = set(NEW_TOOLS)
    if expect_legacy:
        expected |= set(CORE_TOOLS)
        if expect_fringe:
            expected |= set(FRINGE_TOOLS)
    expected.add("bsk_evaluate")

    record(
        f"{label}：注册数 = {len(expected)}",
        got == expected,
        f"实际 {len(got)} 个；多了 {sorted(got - expected)}；少了 {sorted(expected - got)}",
    )
    # 逐组单独报，失败时能一眼看出是哪一组的问题。
    record(
        f"{label}：7 个新工具齐全",
        set(NEW_TOOLS) <= got,
        f"缺 {sorted(set(NEW_TOOLS) - got)}",
    )
    record(
        f"{label}：core 4 个{'在' if expect_legacy else '不在'}注册表里",
        (set(CORE_TOOLS) <= got) if expect_legacy else not (set(CORE_TOOLS) & got),
        f"core 实际命中 {sorted(set(CORE_TOOLS) & got)}",
    )
    record(
        f"{label}：fringe 3 个{'在' if expect_fringe and expect_legacy else '不在'}注册表里",
        (set(FRINGE_TOOLS) <= got)
        if (expect_legacy and expect_fringe)
        else not (set(FRINGE_TOOLS) & got),
        f"fringe 实际命中 {sorted(set(FRINGE_TOOLS) & got)}",
    )
    record(
        f"{label}：bsk_evaluate 不受这组开关影响（始终在）",
        "bsk_evaluate" in got,
        f"实际 {sorted(n for n in got if n == 'bsk_evaluate') or '不在'}",
    )


async def main() -> int:
    base = {"bsk_path": "bsk", "admin_only": True, "screenshot_path": ""}

    print("=" * 74)
    print("旧工具两级开关：legacy_tools × legacy_fringe_tools")
    print("=" * 74)

    print("\n--- 1. legacy_tools=true, legacy_fringe_tools=true → 15 个 ---")
    got = await observe({**base, "legacy_tools": True, "legacy_fringe_tools": True})
    print(f"    实际注册：{sorted(got)}")
    check_group("组合1 (true,true)", got, expect_legacy=True, expect_fringe=True)

    print("\n--- 2. legacy_tools=true, legacy_fringe_tools=false → 12 个 ---")
    got = await observe({**base, "legacy_tools": True, "legacy_fringe_tools": False})
    print(f"    实际注册：{sorted(got)}")
    check_group("组合2 (true,false)", got, expect_legacy=True, expect_fringe=False)

    print("\n--- 3. legacy_tools=false, legacy_fringe_tools=true → 8 个 ---")
    got = await observe({**base, "legacy_tools": False, "legacy_fringe_tools": True})
    print(f"    实际注册：{sorted(got)}")
    check_group("组合3 (false,true)", got, expect_legacy=False, expect_fringe=False)

    print("\n--- 4. legacy_tools=false, legacy_fringe_tools=false → 8 个 ---")
    got = await observe({**base, "legacy_tools": False, "legacy_fringe_tools": False})
    print(f"    实际注册：{sorted(got)}")
    check_group("组合4 (false,false)", got, expect_legacy=False, expect_fringe=False)

    print("\n--- 5. 默认配置（两项都不填）→ 走 legacy_tools=true + fringe=false ---")
    got = await observe(dict(base))
    print(f"    实际注册：{sorted(got)}")
    check_group("默认配置", got, expect_legacy=True, expect_fringe=False)

    print("\n--- 6. enable_evaluate=true 不改变注册数量（它只是权限门）---")
    got = await observe(
        {
            **base,
            "legacy_tools": True,
            "legacy_fringe_tools": True,
            "enable_evaluate": True,
        }
    )
    record(
        "6.1 enable_evaluate=true 时仍是 15 个（注册期不变）",
        len(got) == 15,
        f"实际 {len(got)} 个",
    )
    record(
        "6.2 enable_evaluate=true 时 bsk_evaluate 仍在",
        "bsk_evaluate" in got,
        "",
    )

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 74)
    print(f"实测项：{len(RESULTS)}，失败：{len(failed)}")
    for name in failed:
        print(f"  - {name}")
    print("结果：" + ("全部通过" if not failed else "有失败"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
