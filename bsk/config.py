"""配置解析与校验。

AstrBot 把用户在 WebUI 里填的配置以**原始 dict** 交给插件（``Star.__init__`` 的
``config`` 参数），用户填什么就传什么：数字可能写成字符串 ``"60"``、开关可能写成
字符串 ``"true"``、字段可能整条缺失、也可能被手工编辑成 ``null`` 或别的类型。
所以这一层的唯一目标是：

    **把任意输入收敛成强类型、取值合法、可直接使用的 :class:`Settings`，永不抛异常。**

两个函数的职责严格分开：

- :func:`parse_settings` —— **兜底**：非法值静默回退默认值，数值越界夹到边界。
- :func:`validate_settings` —— **报告**：把"值合法、但用起来会出问题"的情况
  （例如超时设得比 AstrBot 自己的调用上限还大）变成给用户看的中文提示，
  供插件启动时打印。

由于 ``parse_settings`` 已经把越界值夹住了，``validate_settings`` 里那些
"≥ 上限"的判断主要面向**直接构造出来的 Settings**（单元测试、脚本、以及按旧
schema 手写的配置文件）。这也是它必须对任意字段取值都不抛异常的原因：
它在插件启动路径上跑，崩了就等于插件加载失败。

本模块**零 astrbot 依赖、零第三方依赖**，可以脱离框架直接单测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ASTRBOT_TOOL_TIMEOUT_LIMIT_SEC",
    "BSK_SESSION_RECLAIM_SEC",
    "COMMAND_TIMEOUT_MAX_SEC",
    "COMMAND_TIMEOUT_MIN_SEC",
    "DEFAULT_ADMIN_ONLY",
    "DEFAULT_ALLOWED_USERS",
    "DEFAULT_BROWSER_INSTANCE_ID",
    "DEFAULT_BSK_PATH",
    "DEFAULT_COMMAND_TIMEOUT_SEC",
    "DEFAULT_ENABLED",
    "DEFAULT_IDLE_RELEASE_SEC",
    "DEFAULT_MAX_PAGE_CHARS",
    "DEFAULT_MAX_SESSIONS",
    "DEFAULT_SCREENSHOT_DIR",
    "DEFAULT_SESSION_SCOPE",
    "IDLE_RELEASE_MAX_SEC",
    "IDLE_RELEASE_MIN_SEC",
    "MAX_PAGE_CHARS_MAX",
    "MAX_PAGE_CHARS_MIN",
    "MAX_SESSIONS_MAX",
    "MAX_SESSIONS_MIN",
    "MAX_SESSIONS_WARN_AT",
    "SESSION_SCOPES",
    "Settings",
    "parse_settings",
    "validate_settings",
]

# 注意：``_as_bool`` 等辅助函数刻意不放进 ``__all__`` —— 它们是本模块的实现细节。
# 需要布尔容错的其他模块请用 ``models._as_bool``，别从这里 import 私有名字。

# --------------------------------------------------------------------------
# 默认值
#
# ⚠️ 这里的每一个默认值都必须与 ``_conf_schema.json`` 里的 ``default`` 完全一致，
#    否则用户在 WebUI 看到的和插件实际用的会对不上，非常难排查。
#    ``tests/test_config.py`` 里有一条测试专门比对这两者，改一边就会红。
# --------------------------------------------------------------------------

DEFAULT_ENABLED = True
"""插件总开关。"""

DEFAULT_BSK_PATH = "bsk"
"""bsk 可执行文件路径。填裸名字表示从 PATH 查找。"""

DEFAULT_BROWSER_INSTANCE_ID = ""
"""目标浏览器实例 ID。空 = 自动选择唯一已连接的浏览器。"""

DEFAULT_COMMAND_TIMEOUT_SEC = 60.0
"""单条 bsk 命令的超时（秒）。必须 < 120，见 ``ASTRBOT_TOOL_TIMEOUT_LIMIT_SEC``。"""

DEFAULT_MAX_SESSIONS = 3
"""同时可存在的浏览器会话数上限。"""

DEFAULT_ADMIN_ONLY = True
"""只允许 AstrBot 管理员调用。默认开启是安全要求，不是可选项。"""

DEFAULT_ALLOWED_USERS: tuple[str, ...] = ()
"""额外允许的用户 ID 白名单。空元组 = 不额外放行任何人。"""

DEFAULT_SESSION_SCOPE = "umo"
"""会话隔离粒度：每个聊天会话一条浏览器会话。"""

DEFAULT_IDLE_RELEASE_SEC = 240.0
"""空闲多久主动释放会话（秒）。必须 < 300，见 ``BSK_SESSION_RECLAIM_SEC``。"""

DEFAULT_SCREENSHOT_DIR = ""
"""截图存放目录。空 = 用插件自己的数据目录。"""

DEFAULT_MAX_PAGE_CHARS = 3000
"""回给模型的页面文本上限（字符数），防止把整棵 VOM 树塞进上下文。"""

# --------------------------------------------------------------------------
# 取值边界与外部约束
# --------------------------------------------------------------------------

ASTRBOT_TOOL_TIMEOUT_LIMIT_SEC = 120.0
"""AstrBot 单次工具调用的时间上限（秒）。

这是**框架侧**的硬限制：到点就会掐断本次工具调用。
所以我们自己的 ``command_timeout_sec`` 必须明显小于它，让 bsk 先超时、
由我们把超时翻译成一句模型能看懂的中文，而不是被框架从外面打断。
"""

BSK_SESSION_RECLAIM_SEC = 300.0
"""bsk 自身回收空闲会话的时间（秒）。

超过这个时间没用过，会话在 bsk 那边就没了。因此空闲释放必须设得更早，
由我们主动、可控地释放，而不是等 bsk 把会话抽走后再被动重建。
"""

COMMAND_TIMEOUT_MIN_SEC = 5.0
COMMAND_TIMEOUT_MAX_SEC = 110.0
"""命令超时的夹取区间。

上界取 110 而不是 119：留 10 秒余量给 AstrBot 侧的结果处理与网络往返，
避免出现"bsk 刚好返回、框架已经掐断"的临界情况。
"""

IDLE_RELEASE_MIN_SEC = 60.0
IDLE_RELEASE_MAX_SEC = 299.0
"""空闲释放的夹取区间。上界必须严格小于 ``BSK_SESSION_RECLAIM_SEC``。"""

MAX_SESSIONS_MIN = 1
MAX_SESSIONS_MAX = 10
"""并发会话数的夹取区间。"""

MAX_SESSIONS_WARN_AT = 5
"""超过这个数量就提醒用户：会开很多浏览器窗口。"""

MAX_PAGE_CHARS_MIN = 200
MAX_PAGE_CHARS_MAX = 20000
"""页面文本上限的夹取区间。下界保证页面至少还有可读内容。"""

SESSION_SCOPES: tuple[str, ...] = ("umo", "user")
"""``session_scope`` 的全部合法取值。"""

# --------------------------------------------------------------------------
# 单个取值的容错转换
#
# 全部遵循同一个约定：**拿不准就返回 default**，绝不抛异常。
# --------------------------------------------------------------------------

# 字符串形式的布尔值。用户把开关填成字符串是很常见的（配置文件手改、
# 从别处复制粘贴），所以必须认这些写法。
_TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on", "y", "t", "是", "开", "启用"})
_FALSY_STRINGS = frozenset({"0", "false", "no", "off", "n", "f", "否", "关", "禁用"})
# 注意空字符串**不在**上面两个集合里：``enabled: ""`` 说明用户没填（或填错了），
# 这时回退默认值比擅自理解成 False 更符合预期。


def _as_bool(value: Any, default: bool) -> bool:
    """把任意值转成布尔值，无法判断时返回 ``default``。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUTHY_STRINGS:
            return True
        if token in _FALSY_STRINGS:
            return False
        return default
    if isinstance(value, (int, float)):
        # 数字按 C 的习惯理解：0 为假，非 0 为真。
        if isinstance(value, float) and not math.isfinite(value):
            return default
        return bool(value)
    return default


def _as_str(value: Any, default: str) -> str:
    """把任意值转成字符串，无法判断时返回 ``default``。

    只接受真正的字符串：数字、列表、字典填在"路径"这种字段上都是明显的误操作，
    这时候回退默认值比 ``str(value)`` 出一个 ``"123"`` 更不容易造成困惑。
    返回值会去掉首尾空白。
    """
    if not isinstance(value, str):
        return default
    token = value.strip()
    return token if token else default


def _as_float(value: Any, default: float) -> float:
    """把任意值转成浮点数，无法判断时返回 ``default``。

    接受数字与"看起来像数字的字符串"（``"60"``、``" 60.5 "``）。
    ``bool`` 被显式排除：``True`` 作为超时时间没有意义，应该走默认值。
    ``nan`` / ``inf`` 也排除 —— 它们参与 ``min``/``max`` 夹取时结果不可靠。
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return default
    else:
        return default
    return result if math.isfinite(result) else default


def _as_int(value: Any, default: int) -> int:
    """把任意值转成整数，无法判断时返回 ``default``。

    小数向零截断（``3.7`` → ``3``）：这类字段是"个数"，截断比四舍五入更保守。
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return int(_as_float(value, float(default)))


def _clamp(value: float, low: float, high: float) -> float:
    """把数值夹到 ``[low, high]``。"""
    return max(low, min(high, value))


def _num(value: Any) -> float | None:
    """把字段值归一化成可用于比较的浮点数，类型不对时返回 ``None``。

    :func:`validate_settings` 专用：它要在"字段被塞了字符串/None"时也能安全比较。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _parse_allowed_users(value: Any) -> tuple[str, ...]:
    """把白名单字段的三种写法统一成 ``tuple[str, ...]``。

    支持：

    - 列表（WebUI 的 list 控件）：``["123", "456"]``
    - 逗号分隔字符串：``"123, 456"``
    - 单个字符串：``"123"``
    - JSON 数字形式的用户 ID：``[123, 456]`` → ``("123", "456")``

    处理规则：去首尾空白、丢掉空项、按出现顺序去重。不认识的结构（字典、
    嵌套列表等）整项忽略，而不是整条配置回退 —— 用户填对了 9 个、填错 1 个时，
    丢掉那 1 个比全部作废更符合预期。
    """
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        return ()

    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, bool):
            # True/False 不可能是用户 ID，几乎一定是填错了。
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                continue
            # 12345.0 这种要还原成 "12345"，否则和 AstrBot 的 sender_id 对不上。
            token = str(int(item)) if item.is_integer() else str(item)
        elif isinstance(item, int):
            token = str(item)
        elif isinstance(item, str):
            token = item.strip()
        else:
            continue
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return tuple(result)


def _parse_session_scope(value: Any) -> str:
    """解析会话隔离粒度，只接受 ``umo`` / ``user``，其余回退默认值。

    大小写与空白不敏感（``" UMO "`` 也算合法）。
    """
    if isinstance(value, str):
        token = value.strip().lower()
        if token in SESSION_SCOPES:
            return token
    return DEFAULT_SESSION_SCOPE


# --------------------------------------------------------------------------
# 强类型配置
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Settings:
    """插件运行所需的全部配置，**保证每一项都已合法**。

    这个对象是不可变的：配置在插件启动时解析一次，运行期只读。
    要改配置就改 WebUI 然后重载插件 —— 避免"运行到一半规则变了"这种难查的行为。

    正常入口是 :func:`parse_settings`；测试或工具代码可以直接构造，
    此时应当再调一次 :func:`validate_settings` 看看有没有值得提醒的问题。
    """

    enabled: bool
    """插件总开关。关闭后所有工具直接拒绝调用。"""

    bsk_path: str
    """bsk 可执行文件路径。裸名字（如 ``bsk``）表示从 PATH 查找，否则视为绝对/相对路径。"""

    browser_instance_id: str
    """目标浏览器的 instance_id。空字符串 = 自动选择唯一已连接的浏览器。"""

    command_timeout_sec: float
    """单条 bsk 命令的超时（秒）。"""

    max_sessions: int
    """同时可存在的浏览器会话数上限。"""

    admin_only: bool
    """是否只允许 AstrBot 管理员调用。"""

    allowed_users: tuple[str, ...]
    """额外放行的用户 ID 白名单（空 = 不额外放行）。"""

    session_scope: str
    """会话隔离粒度，``"umo"``（每个聊天会话一条）或 ``"user"``（每人一条）。"""

    idle_release_sec: float
    """空闲多久主动释放浏览器会话（秒）。"""

    screenshot_dir: str
    """截图存放目录。空字符串 = 用插件自己的数据目录。"""

    max_page_chars: int
    """回给模型的页面文本上限（字符数）。"""


def parse_settings(raw: dict | None) -> Settings:
    """从 AstrBot 的原始配置 dict 构造 :class:`Settings`。

    **任何非法值都回退到默认值，任何越界值都夹到边界，本函数永不抛异常。**
    用户把配置填成一团乱麻的后果是"插件按默认值工作"，而不是"插件起不来"。

    Args:
        raw: AstrBot 传来的原始配置。``None``、空 dict、甚至根本不是 dict
            都会被当成"没有配置"处理，全部走默认值。

    Returns:
        校验后的配置对象。
    """
    if not isinstance(raw, dict):
        # AstrBot 正常会传 dict（AstrBotConfig 是 dict 子类），这里只是兜底。
        raw = {}

    screenshot_dir = raw.get("screenshot_dir")
    return Settings(
        enabled=_as_bool(raw.get("enabled"), DEFAULT_ENABLED),
        bsk_path=_as_str(raw.get("bsk_path"), DEFAULT_BSK_PATH),
        browser_instance_id=_as_str(
            raw.get("browser_instance_id"), DEFAULT_BROWSER_INSTANCE_ID
        ),
        command_timeout_sec=_clamp(
            _as_float(raw.get("command_timeout_sec"), DEFAULT_COMMAND_TIMEOUT_SEC),
            COMMAND_TIMEOUT_MIN_SEC,
            COMMAND_TIMEOUT_MAX_SEC,
        ),
        max_sessions=int(
            _clamp(
                _as_int(raw.get("max_sessions"), DEFAULT_MAX_SESSIONS),
                MAX_SESSIONS_MIN,
                MAX_SESSIONS_MAX,
            )
        ),
        admin_only=_as_bool(raw.get("admin_only"), DEFAULT_ADMIN_ONLY),
        allowed_users=_parse_allowed_users(raw.get("allowed_users")),
        session_scope=_parse_session_scope(raw.get("session_scope")),
        idle_release_sec=_clamp(
            _as_float(raw.get("idle_release_sec"), DEFAULT_IDLE_RELEASE_SEC),
            IDLE_RELEASE_MIN_SEC,
            IDLE_RELEASE_MAX_SEC,
        ),
        # 空字符串在这里是有意义的取值（表示"用默认目录"），所以不能让
        # _as_str 把它折成默认值 —— 它的默认值本来也就是空字符串，正好一致。
        screenshot_dir=_as_str(screenshot_dir, DEFAULT_SCREENSHOT_DIR),
        max_page_chars=int(
            _clamp(
                _as_int(raw.get("max_page_chars"), DEFAULT_MAX_PAGE_CHARS),
                MAX_PAGE_CHARS_MIN,
                MAX_PAGE_CHARS_MAX,
            )
        ),
    )


def validate_settings(s: Settings) -> list[str]:
    """检查配置里"合法但会出问题"的地方，返回给用户看的中文提示。

    只报告**实质问题**，不报告非法值 —— 非法值已经在 :func:`parse_settings`
    里被兜底了，那些不需要用户操心。

    Args:
        s: 待检查的配置。

    Returns:
        问题列表，空列表表示没有问题。每一项都是完整、可独立阅读的一句话，
        包含"哪里不对"和"该怎么办"。
    """
    problems: list[str] = []

    # 这里对每个字段都先用 _num/_count 归一化再过判断，而不是直接比较：
    # 本函数在插件启动路径上跑，一个字段类型不对就会让异常冒到框架，
    # 结果整个插件加载失败 —— 那正是我们要避免的失败模式。
    timeout = _num(s.command_timeout_sec)
    if timeout is not None and timeout >= ASTRBOT_TOOL_TIMEOUT_LIMIT_SEC:
        problems.append(
            f"`command_timeout_sec` 设成了 {timeout:g} 秒，超过了 AstrBot 单次工具调用"
            f"的 {ASTRBOT_TOOL_TIMEOUT_LIMIT_SEC:g} 秒上限。AstrBot 会在命令返回之前就掐断"
            "这次调用，机器人只会看到一句失败，而浏览器那边可能还在动。"
            f"建议改到 {DEFAULT_COMMAND_TIMEOUT_SEC:g} 秒左右"
            "（插件最多只接受 110 秒）；整页截图这类耗时操作请拆成多步。"
        )

    idle = _num(s.idle_release_sec)
    if idle is not None and idle >= BSK_SESSION_RECLAIM_SEC:
        problems.append(
            f"`idle_release_sec` 设成了 {idle:g} 秒，但 bsk 自己会在会话空闲 5 分钟"
            f"（{BSK_SESSION_RECLAIM_SEC:g} 秒）时把会话回收掉，所以设得再大也没有用："
            "插件会拿着一个已经被回收的会话去操作，失败后再重建，白多一次往返。"
            f"建议设在 {DEFAULT_IDLE_RELEASE_SEC:g} 秒以内（插件最多只接受 299 秒）。"
        )

    if not s.admin_only:
        problems.append(
            "【安全提醒】`admin_only` 已经关闭：**任何能给机器人发消息的人**都能操控"
            "你这台机器上已登录的浏览器 —— 读你的网页、点按钮、填表单、截图。"
            "如果只是想给某个人用，请把 `admin_only` 改回开启，并把对方的用户 ID"
            "填进 `allowed_users`。"
        )

    max_sessions = _num(s.max_sessions)
    if max_sessions is not None and max_sessions > MAX_SESSIONS_WARN_AT:
        problems.append(
            f"`max_sessions` 设成了 {max_sessions:g}，同时在聊的用户多时会开"
            f"{max_sessions:g} 个浏览器窗口，比较打扰人，也更容易撞上 bsk 的同时只允许"
            "一个在途命令的限制。建议不要超过 5。"
        )

    # 复用解析器做归一化：无论用户把白名单填成什么形态，这里都能安全地数出条数。
    users = _parse_allowed_users(s.allowed_users)
    if users and s.admin_only:
        shown = "、".join(users[:5]) + ("…" if len(users) > 5 else "")
        problems.append(
            f"`allowed_users` 里填了 {len(users)} 个用户（{shown}），但 `admin_only` "
            "仍然是开启的。这两个条件是「取严」关系：管理员之外的人即使在白名单里，"
            "也会先被管理员检查挡下来，所以这份白名单**不会生效**。"
            "想让白名单里的非管理员也能用，需要把 `admin_only` 关掉"
            "（注意上面那条安全提醒）。"
        )

    return problems
