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

    def is_admin(self) -> bool:
        return self._is_admin

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_session_id(self) -> str:
        return self._session_id

    def image_result(self, path: str) -> Any:
        """返回一个可识别的标记，便于断言图片确实被 yield 了。"""

        class _ImageResult:
            def __init__(self, p: str) -> None:
                self.path = p

            def __repr__(self) -> str:
                return f"<ImageResult {self.path}>"

        return _ImageResult(path)
