"""L2 契约测试的辅助模块：AstrBot 侧的最小测试替身。

为什么需要它：真实的 ``astrbot.api.star.Context`` 需要 12 个重量级依赖
（事件队列、数据库、各管理器……），在契约测试里没有理由把它们都造出来。
插件对 Context 的使用非常有限（只是传给 ``Star.__init__``），
所以用一个"鸭子类型"的替身即可，同时保证替身不掩盖真实的接口错误 ——
如果插件访问了 Context 上不存在的属性，这里会抛 AttributeError。
"""

from __future__ import annotations

from typing import Any


class FakeProviderManager:
    """``context.provider_manager`` 的最小替身。"""

    def __init__(self) -> None:
        self.llm_tools = _FakeToolRegistry()


class _FakeToolRegistry:
    def __init__(self) -> None:
        self.func_list: list[Any] = []

    def remove_func(self, name: str) -> None:
        self.func_list = [t for t in self.func_list if getattr(t, "name", None) != name]

    def get_func(self, name: str) -> Any:
        for t in self.func_list:
            if getattr(t, "name", None) == name:
                return t
        return None


class FakeContext:
    """``astrbot.api.star.Context`` 的测试替身。

    只提供插件实际用到的东西，其余访问会抛 ``AttributeError`` ——
    这是刻意的：宁可测试失败，也不要让替身掩盖了真实的接口差异。
    """

    def __init__(self) -> None:
        self.provider_manager = FakeProviderManager()
        self._config: dict[str, Any] = {}

    def get_config(self) -> dict[str, Any]:
        return self._config

    def add_llm_tools(self, *tools: Any) -> None:
        for tool in tools:
            self.provider_manager.llm_tools.func_list.append(tool)

    async def send_message(self, *args: Any, **kwargs: Any) -> bool:
        return True


def make_context() -> FakeContext:
    """构造一个可用的假 Context。"""
    return FakeContext()


class FakeEvent:
    """``AstrMessageEvent`` 的测试替身，用于验证权限门与会话键。"""

    def __init__(
        self,
        *,
        is_admin: bool = False,
        sender_id: str = "10001",
        umo: str = "aiocqhttp:group:12345",
        session_id: str = "12345",
    ) -> None:
        self._is_admin = is_admin
        self._sender_id = sender_id
        self.unified_msg_origin = umo
        self._session_id = session_id
        self._extras: dict[str, Any] = {}

    def is_admin(self) -> bool:
        return self._is_admin

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_session_id(self) -> str:
        return self._session_id

    # ``bsk_load_tools`` 靠这两个方法取本轮的 ``ProviderRequest``
    # （真实实现见 ``astr_message_event.py``）。替身必须提供它们，
    # 否则测到的是 AttributeError 而不是真实行为。
    #
    # 默认**不预置** ``provider_request``：这让"拿不到请求上下文"这条
    # 降级路径可以被显式测到，而不是被替身悄悄补上一个假对象掩盖过去。
    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value

    def get_extra(self, key: str | None = None, default: Any = None) -> Any:
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def image_result(self, path: str) -> Any:
        """返回一个可识别的标记，便于断言图片确实被 yield 了。"""

        class _ImageResult:
            def __init__(self, p: str) -> None:
                self.path = p

            def __repr__(self) -> str:
                return f"<ImageResult {self.path}>"

        return _ImageResult(path)


def ensure_astrbot_modules() -> None:
    """在无真实 AstrBot 时注入最小替身模块，供单元测试加载 main.py。"""
    import sys
    import types

    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")

    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    provider = types.ModuleType("astrbot.core.provider")
    provider.__path__ = []
    register_mod = types.ModuleType("astrbot.core.provider.register")
    agent = types.ModuleType("astrbot.core.agent")
    agent.__path__ = []
    tool_mod = types.ModuleType("astrbot.core.agent.tool")
    utils = types.ModuleType("astrbot.core.utils")
    utils.__path__ = []
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")

    class ToolSet:
        pass

    tool_mod.ToolSet = ToolSet
    path_mod.get_astrbot_data_path = lambda: "/tmp/fake_astrbot_data"

    fake_registry = _FakeToolRegistry()
    register_mod.llm_tools = fake_registry

    provider.register = register_mod
    core.provider = provider
    agent.tool = tool_mod
    core.agent = agent
    utils.astrbot_path = path_mod
    core.utils = utils
    astrbot.core = core

    class Star:
        def __init__(self, *a, **k):
            pass

    class AstrMessageEvent:
        pass

    class Filter:
        def llm_tool(self, *a, **k):
            def dec(f):
                return f

            return dec

        def event_message_type(self, *a, **k):
            def dec(f):
                return f

            return dec

        def command(self, *a, **k):
            def dec(f):
                return f

            return dec

        def command_group(self, *a, **k):
            def dec(f):
                return f

            return dec

    filter = Filter()

    class StarTools:
        @staticmethod
        def get_data_dir(*a, **k):
            return ""

    class Context:
        pass

    event.AstrMessageEvent = AstrMessageEvent
    event.filter = filter
    event.EventMessageType = type("EMT", (), {"ALL": 1})
    star.Star = Star
    star.Context = Context
    star.StarTools = StarTools
    star.register = lambda *a, **k: (lambda cls: cls)
    api.logger = type(
        "Logger",
        (),
        {
            "info": lambda *a, **k: None,
            "warning": lambda *a, **k: None,
            "debug": lambda *a, **k: None,
            "exception": lambda *a, **k: None,
        },
    )()
    api.event = event
    api.star = star
    astrbot.api = api

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.provider"] = provider
    sys.modules["astrbot.core.provider.register"] = register_mod
    sys.modules["astrbot.core.agent"] = agent
    sys.modules["astrbot.core.agent.tool"] = tool_mod
    sys.modules["astrbot.core.utils"] = utils
    sys.modules["astrbot.core.utils.astrbot_path"] = path_mod

