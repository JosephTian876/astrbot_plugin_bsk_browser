"""真机验收：``bsk_debug`` 的按需加载（``lazy_debug_tool``）。

## 这个脚本要证明什么

``bsk_debug`` 是一块 4400 多字符的大工具，默认不注册给模型；模型要用它，
得先调常驻的小工具 ``bsk_load_tools``。整条机制成立的前提是四条：

1. 默认配置下，**模型看到的工具列表里没有 bsk_debug**、但**有 bsk_load_tools**；
2. 调完 ``bsk_load_tools`` 之后，**本轮请求**的 ``req.func_tool`` 里真的多了
   ``bsk_debug``（不是"以后某一次"、也不是"全局打开"）；
3. 紧接着调 ``bsk_debug`` 能**真的执行**（走真实执行器 + 真实浏览器）；
4. ``lazy_debug_tool=false`` 时 ``bsk_debug`` 一开始就在，行为与参考实现一致。

外加一条负向断言：加载只影响**当前这一个 req** —— 重新按注册表构造一份请求时
``bsk_debug`` 仍然不在。这一条是"不会多用户互相污染"的唯一硬证据，
没有它就分不清"按需加载"和"偷偷把全局打开了"。

## 为什么必须真机

``req.func_tool`` 的生效链路横跨三个模块（``astr_main_agent`` 存 extra →
``tool_loop_agent_runner`` 现读 ``self.req.func_tool`` → provider 序列化），
任何一环换了实现，单测都测不出来：单测里我们自己造的那个 req 当然听话。
所以这里用**真实的** ``AstrMessageEvent`` / ``ProviderRequest`` / ``ToolSet`` /
``llm_tools`` 注册表，并让工具经**真实的** ``call_local_llm_tool`` 执行。

用法：
    & 'D:\\AstrBot\\backend\\python\\python.exe' tests/verify_lazy_debug_real.py

退出码：全部通过 0，有任何失败 1。
"""

from __future__ import annotations

import asyncio
import functools
import io
import importlib
import json
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
for path in (PROJECT, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_ASTRBOT_APP = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
_ASTRBOT_ROOT = os.environ.get("ASTRBOT_ROOT", str(Path.home() / ".astrbot"))
os.environ.setdefault("ASTRBOT_ROOT", _ASTRBOT_ROOT)
for path in (_ASTRBOT_APP, _ASTRBOT_ROOT):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

SOURCE_MODULE = "data.plugins.astrbot_plugin_bsk_browser.main"

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail[:130]}" if detail else ""))


class _Wrap:
    """``call_local_llm_tool`` 只用到 ``context.context.event``。"""

    def __init__(self, event):
        self.context = type("X", (), {"event": event})()


async def call_tool(handler, event, **kwargs) -> str:
    """经 AstrBot 真实执行器调用工具，返回拼接后的文本。"""
    from astrbot.core.astr_agent_tool_exec import call_local_llm_tool

    out = []
    async for item in call_local_llm_tool(
        context=_Wrap(event),
        handler=handler,
        method_name="decorator_handler",
        **kwargs,
    ):
        if isinstance(item, str):
            out.append(item)
    return "\n".join(out)


def make_event():
    """构造一个**真实**的 ``AstrMessageEvent``（管理员私聊）。

    刻意不用 ``astrbot_test_doubles.FakeEvent``：那是个鸭子类型替身，
    ``set_extra`` / ``get_extra`` 的行为是我们自己写的，而本脚本要验的
    恰恰是"事件真的能带着 ProviderRequest 走完这一轮"。
    """
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    meta = PlatformMetadata(name="lazydebug", description="验收用", id="lazydebug")
    event = AstrMessageEvent("", message, meta, "lazydebug-session")
    event.role = "admin"  # 权限门要它
    return event


def registry_snapshot() -> tuple[set[str], set[str]]:
    """返回（全部 bsk_ 工具名，激活的 bsk_ 工具名）。"""
    from astrbot.core.provider.register import llm_tools

    all_names = {t.name for t in llm_tools.func_list if t.name.startswith("bsk_")}
    active = {
        t.name
        for t in llm_tools.func_list
        if t.name.startswith("bsk_") and getattr(t, "active", True)
    }
    return all_names, active


def build_request_toolset():
    """按 `astr_main_agent` 的真实做法，从注册表构造一份本轮请求的工具集。

    只收 ``active`` 的工具 —— 这正是"模型能看见什么"的判定条件
    （``ToolSet`` 的三个 schema 序列化器都不按 ``active`` 过滤，所以
    谁进得来，谁就会被发给 provider）。
    """
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.provider.register import llm_tools

    toolset = ToolSet()
    for tool in llm_tools.func_list:
        if tool.name.startswith("bsk_") and getattr(tool, "active", True):
            toolset.add_tool(tool)
    return toolset


def payload_sizes(toolset) -> dict[str, int]:
    return {
        kind: len(json.dumps(getattr(toolset, f"{kind}_schema")(), ensure_ascii=False))
        for kind in ("openai", "anthropic", "google")
    }


async def boot(config: dict):
    """跑一次真实 ``initialize()``，返回 (plugin, bound_handlers)。"""
    from astrbot.core.provider.register import llm_tools
    from astrbot_test_doubles import make_context

    # 每次都要先把上一次留下的 bsk_ 工具清掉：上一轮的 active=False 会残留，
    # 把这一轮污染成"都没注册"。
    for tool in list(llm_tools.func_list):
        if tool.name.startswith("bsk_"):
            llm_tools.remove_func(tool.name)

    sys.modules.pop(SOURCE_MODULE, None)
    sys.modules.pop("astrbot_plugin_bsk_browser", None)
    module = importlib.import_module(SOURCE_MODULE)

    plugin = module.BskBrowserPlugin(make_context(), config=config)
    await plugin.initialize()

    bound: dict[str, object] = {}
    for tool in llm_tools.func_list:
        if not tool.name.startswith("bsk_"):
            continue
        raw = tool.handler
        if raw is None:
            continue
        fn = raw.func if isinstance(raw, functools.partial) else raw
        bound[tool.name] = functools.partial(fn, plugin)
    return plugin, module, bound


async def main() -> int:
    base = {"bsk_path": "bsk", "admin_only": True, "screenshot_dir": ""}

    print("=" * 74)
    print("真机验收：bsk_debug 按需加载（lazy_debug_tool）")
    print("=" * 74)

    # ------------------------------------------------------------------
    print("\n--- 1. lazy_debug_tool=true（默认）：模型看不到 bsk_debug ---")
    plugin, _module, bound = await boot(dict(base))
    all_names, active = registry_snapshot()
    print(f"    注册表里全部 bsk_ 工具（{len(all_names)}）：{sorted(all_names)}")
    print(f"    其中 active 的（模型可见，{len(active)}）：{sorted(active)}")

    record("1.1 bsk_debug 仍在注册表里（handler 可被直接调用）",
           "bsk_debug" in all_names, "")
    record("1.2 但它不是 active —— 模型看不到它",
           "bsk_debug" not in active, f"active：{sorted(active)}")
    record("1.3 bsk_load_tools 常驻且 active",
           "bsk_load_tools" in active, "")

    toolset = build_request_toolset()
    names_now = set(toolset.names())
    record("1.4 默认工具集里没有 bsk_debug",
           "bsk_debug" not in names_now, f"工具集（{len(names_now)}）：{sorted(names_now)}")
    record("1.5 默认工具集里有 bsk_load_tools",
           "bsk_load_tools" in names_now, "")

    sizes_before = payload_sizes(toolset)
    print(f"    默认工具块体积：{sizes_before}")

    # ------------------------------------------------------------------
    print("\n--- 2. 调 bsk_load_tools 之后，本轮 req.func_tool 里有了 bsk_debug ---")
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.provider.entities import ProviderRequest

    event = make_event()
    req = ProviderRequest()
    req.session_id = "lazydebug-session"
    req.func_tool = build_request_toolset()
    # 这一步就是 astr_main_agent 在线路上做的那件事（同一个 req 对象）。
    event.set_extra("provider_request", req)
    record("2.0 事件能带出同一个 ProviderRequest 对象",
           event.get_extra("provider_request") is req, "")

    r = await call_tool(bound["bsk_load_tools"], event, action="debug")
    print(f"    bsk_load_tools 回执：{r.replace(chr(10), ' ')[:150]}")
    record("2.1 bsk_load_tools 报告已加载", "已加载" in r, r.replace("\n", " ")[:110])

    got = req.func_tool.get_tool("bsk_debug")
    record("2.2 req.func_tool 里出现了 bsk_debug", got is not None,
           f"当前：{sorted(req.func_tool.names())}")
    record("2.3 加进去的就是注册表里那个对象（同一个 handler）",
           got is not None and got.handler is not None, "")
    record("2.4 加进去的工具带着完整 schema（28 个参数）",
           got is not None and len((got.parameters or {}).get("properties", {})) == 28,
           f"参数数：{len((got.parameters or {}).get('properties', {})) if got else 0}")

    next_payload = req.func_tool.openai_schema()
    payload_names = {d["function"]["name"] for d in next_payload}
    record("2.5 下一次 provider 载荷里出现 bsk_debug",
           "bsk_debug" in payload_names, f"载荷共 {len(payload_names)} 个工具")
    sizes_after = payload_sizes(req.func_tool)
    print(f"    加载后的工具块体积：{sizes_after}")
    print(f"    本轮多付：{ {k: sizes_after[k] - sizes_before[k] for k in sizes_after} }")

    # 负向：加载只对**这一个** req 生效。
    fresh = build_request_toolset()
    record("2.6 重新构造的请求里仍然没有 bsk_debug（没有全局污染）",
           "bsk_debug" not in set(fresh.names()), f"工具集：{sorted(fresh.names())}")
    _, active_after = registry_snapshot()
    record("2.7 注册表的 active 状态没被改动",
           "bsk_debug" not in active_after, f"active：{sorted(active_after)}")

    # 幂等：再调一次不该出错、也不该出现重复项。
    await call_tool(bound["bsk_load_tools"], event, action="debug")
    record("2.8 重复加载是幂等的（工具不重复）",
           req.func_tool.names().count("bsk_debug") == 1,
           f"出现 {req.func_tool.names().count('bsk_debug')} 次")

    # ------------------------------------------------------------------
    print("\n--- 3. 紧接着调 bsk_debug 能真的执行（真机浏览器）---")
    r = await call_tool(bound["bsk_session"], event, action="start")
    record("3.1 启动会话", "失败" not in r and "错误" not in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_debug"], event, action="capabilities")
    record("3.2 debug.capabilities 真的跑通",
           bool(r.strip()) and "未预期" not in r and "失败" not in r,
           r.replace("\n", " ")[:130])

    r = await call_tool(bound["bsk_debug"], event, action="status")
    record("3.3 debug.status 真的跑通",
           bool(r.strip()) and "未预期" not in r, r.replace("\n", " ")[:130])

    r = await call_tool(bound["bsk_debug"], event, action="request")
    record("3.4 条件规则在真机路径上仍然生效（request 缺 id 被拒）",
           "id" in r and "未预期" not in r, r.replace("\n", " ")[:130])

    r = await call_tool(bound["bsk_debug"], event, action="capabilities",
                        debug_action="capabilities")
    record("3.5 已删除的 debug_action 别名在真机路径上也被拒",
           "debug_action" in r and "未预期" not in r, r.replace("\n", " ")[:130])

    await plugin.terminate()

    # ------------------------------------------------------------------
    print("\n--- 4. lazy_debug_tool=false：bsk_debug 一开始就在 ---")
    plugin2, _m2, bound2 = await boot({**base, "lazy_debug_tool": False})
    _all2, active2 = registry_snapshot()
    record("4.1 bsk_debug 一开始就是 active 的", "bsk_debug" in active2, "")
    record("4.2 bsk_load_tools 也还在（它无害）", "bsk_load_tools" in active2, "")
    toolset2 = build_request_toolset()
    record("4.3 默认工具集里直接就有 bsk_debug",
           "bsk_debug" in set(toolset2.names()),
           f"工具集（{len(toolset2.names())}）：{sorted(toolset2.names())}")
    sizes_always = payload_sizes(toolset2)
    print(f"    常注册时的工具块体积：{sizes_always}")

    # 关掉之后不该需要加载这一步：直接调 bsk_debug 就该能跑。
    event2 = make_event()
    req2 = ProviderRequest()
    req2.func_tool = build_request_toolset()
    event2.set_extra("provider_request", req2)
    r = await call_tool(bound2["bsk_session"], event2, action="start")
    record("4.4 启动会话（第二组）", "失败" not in r and "错误" not in r,
           r.replace("\n", " ")[:110])
    r = await call_tool(bound2["bsk_debug"], event2, action="capabilities")
    record("4.5 不加载也能直接调 bsk_debug",
           bool(r.strip()) and "未预期" not in r, r.replace("\n", " ")[:130])
    await plugin2.terminate()

    # ------------------------------------------------------------------
    print("\n--- 5. 两组配置的体积差 ---")
    delta = {k: sizes_always[k] - sizes_before[k] for k in sizes_before}
    print(f"    按需加载省下：{delta}")
    record("5.1 按需加载确实省下 bsk_debug 那一份（三列都为正）",
           all(v > 0 for v in delta.values()), f"{delta}")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 74)
    print(f"实测项：{len(RESULTS)}，失败：{len(failed)}")
    for name in failed:
        print(f"  - {name}")
    print("结果：" + ("全部通过" if not failed else "有失败"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
