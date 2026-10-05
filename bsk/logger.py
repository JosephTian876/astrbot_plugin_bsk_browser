"""bsk 层的日志接口 —— 依赖注入的接缝。

本包有两条必须同时成立的约束，它们把"日志怎么写"这件事夹到了唯一一条路上：

1. 插件市场审核要求 logger 必须来自 ``astrbot.api``，不得使用 Python 内置的
   日志模块；
2. 本包不得依赖 AstrBot（有静态测试守着），否则就没法脱离框架独立单测。

两者同时满足的办法只有一个：本模块只声明"日志该长什么样"，真正的 logger 由
``main.py``（唯一允许依赖框架的文件）取来后通过构造参数注入。没注入时
（单测、脚本、以及解析失败后的降级）一律退回 :data:`NULL_LOGGER`。

调用约定与标准库 logger 一致 —— **惰性 %-格式化**，格式串与参数分开传::

    self._logger.info("会话 %s 已重建为 %s", key, new_id)

本包既有的全部调用点都是这个形态，所以接口必须保持这个签名，不能改成
f-string：那样在日志级别被关掉时白白做一次字符串拼接与格式化。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "NULL_LOGGER",
    "LoggerLike",
    "NullLogger",
]


@runtime_checkable
class LoggerLike(Protocol):
    """``bsk/`` 各组件接受的日志接口（结构类型，不要求继承）。

    刻意用 :class:`~typing.Protocol` 而不是抽象基类：要注入进来的是
    ``astrbot.api.logger``、标准库的 logger、以及测试里的记录型假对象，
    它们都不该为了被本包接受而去改继承关系。方法齐全即算满足。

    方法签名里的 ``*args`` 是必须的 —— 它承载的就是上一条说的惰性
    %-格式化参数（``logger.info("...%s", x)``）。``exception`` 也必须存在：
    ``main.py`` 在几个工具的兜底分支上用它，而且它靠的是"记录当前异常"
    这个语义（调用点不传 ``exc_info``）。

    ``runtime_checkable`` 让"注入的东西像不像 logger"能在测试里直接用
    ``isinstance`` 断言（只看方法名，不看签名）。
    """

    def debug(self, msg: object, *args: Any, **kwargs: Any) -> None: ...

    def info(self, msg: object, *args: Any, **kwargs: Any) -> None: ...

    def warning(self, msg: object, *args: Any, **kwargs: Any) -> None: ...

    def error(self, msg: object, *args: Any, **kwargs: Any) -> None: ...

    def exception(self, msg: object, *args: Any, **kwargs: Any) -> None: ...


class NullLogger:
    """兜底 logger：什么都不做，也永不抛异常。

    为什么不干脆让调用点写 ``if self._logger is not None``：那种判断要重复
    几十次，漏掉一处就是一个 ``AttributeError`` —— 而它偏偏会出现在最不该
    出问题的降级路径上（journal 读写失败、截图清理失败、探测失败……）。
    有了本类，调用点可以无条件地写 ``self._logger.debug(...)``。

    所有参数一律接受、一律忽略：参数个数与类型完全由调用点决定，兜底实现
    若对它们做任何检查（例如只认字符串），就会在"绝不添乱"这件事上反过来
    添乱。这也是它必须显式实现全部五个级别、而不是靠 ``__getattr__`` 兜住
    的原因 —— 显式的方法能撑住 :class:`LoggerLike` 的结构检查。
    """

    __slots__ = ()

    def debug(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """忽略一条 debug 日志。"""

    def info(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """忽略一条 info 日志。"""

    def warning(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """忽略一条 warning 日志。"""

    def error(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """忽略一条 error 日志。"""

    def exception(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """忽略一条 exception 日志。"""


NULL_LOGGER = NullLogger()
"""全局共用的兜底实例（无状态，可以安全共享）。"""
