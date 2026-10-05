"""配置解析与校验。

AstrBot 把用户在 WebUI 里填的配置以原始 dict 交给插件（``Star.__init__`` 的
``config`` 参数），用户填什么就传什么：数字可能写成字符串 ``"60"``、开关可能写成
字符串 ``"true"``、字段可能整条缺失、也可能被手工编辑成 ``null`` 或别的类型。
所以这一层的唯一目标是：

    把任意输入收敛成强类型、取值合法、可直接使用的 :class:`Settings`，永不抛异常。

两个函数的职责严格分开：

- :func:`parse_settings` —— 兜底：非法值静默回退默认值，数值越界夹到边界。
- :func:`validate_settings` —— 报告：把"值合法、但用起来会出问题"的情况
  （例如超时设得比 AstrBot 自己的调用上限还大）变成给用户看的中文提示，
  供插件启动时打印。

由于 ``parse_settings`` 已经把越界值夹住了，``validate_settings`` 里那些
"≥ 上限"的判断主要面向直接构造出来的 Settings（单元测试、脚本、以及按旧
schema 手写的配置文件）。这也是它必须对任意字段取值都不抛异常的原因：
它在插件启动路径上跑，崩了就等于插件加载失败。

本模块零 astrbot 依赖、零第三方依赖，可以脱离框架直接单测。
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
    "DEFAULT_ENABLE_EVALUATE",
    "DEFAULT_EVALUATE_REQUIRE_ADMIN",
    "DEFAULT_FULLPAGE_TIMEOUT_SEC",
    "DEFAULT_IDLE_RELEASE_SEC",
    "DEFAULT_JOURNAL_PATH",
    "DEFAULT_MAX_PAGE_CHARS",
    "DEFAULT_MAX_SESSIONS",
    "DEFAULT_SCREENSHOT_DIR",
    "DEFAULT_SESSION_SCOPE",
    "FRAMEWORK_TIMEOUT_FLOOR_SEC",
    "FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC",
    "FRAMEWORK_TOOL_TIMEOUT_PATH",
    "FULLPAGE_TIMEOUT_MAX_SEC",
    "FULLPAGE_TIMEOUT_MIN_SEC",
    "IDLE_RELEASE_MAX_SEC",
    "IDLE_RELEASE_MIN_SEC",
    "MAX_PAGE_CHARS_MAX",
    "MAX_PAGE_CHARS_MIN",
    "MAX_SESSIONS_MAX",
    "MAX_SESSIONS_MIN",
    "MAX_SESSIONS_WARN_AT",
    "SESSION_SCOPES",
    "Settings",
    "as_timeout_seconds",
    "parse_settings",
    "read_framework_tool_timeout",
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
"""所有 bsk 命令的超时（秒）。必须 < 120，见 ``ASTRBOT_TOOL_TIMEOUT_LIMIT_SEC``。

它是全局超时：每个命令还有一个内置的下限建议值（见 ``service.py`` 的
``TIMEOUT_*``），最终超时 = ``max(内置下限, 本项)`` —— 再对框架上限做一次钳制
（见 ``service.BskService._timeout``）。所以调大它一定能生效，
调小它则不会把慢命令压到下限以下。
"""

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
"""截图存放目录。空 = 插件数据目录下的 ``shots``
（``data/plugin_data/astrbot_plugin_bsk_browser/shots``）。"""

DEFAULT_JOURNAL_PATH = ""
"""会话 journal 文件位置。空 = 插件数据目录下的 ``sessions.json``
（``data/plugin_data/astrbot_plugin_bsk_browser/sessions.json``）。

journal 记录"本插件创建了哪些浏览器会话"。AstrBot 被强杀时 ``terminate()``
不会执行，而 bsk daemon 独立于 AstrBot 继续活着 —— 那些会话（以及用户桌面上的
浏览器窗口）就没人管了。下次启动时插件靠这份记录把它们按 id 精确停掉。

"插件数据目录"由 ``main.py`` 解析后经 ``Settings.data_dir`` 注入；那一步失败时
（数据目录不可写、``StarTools`` 不可用）会降级到系统临时目录下的
``astrbot_bsk_browser/sessions.json``，任何一步都不抛异常。详见 ``bsk/paths.py``。

刻意与 ``screenshot_dir`` 一样用"空 = 用默认位置"的语义：绝大多数用户不需要
关心它，而真正需要排查的人可以填一个固定路径，方便直接打开看。
"""

DEFAULT_MAX_PAGE_CHARS = 3000
"""回给模型的页面文本上限（字符数），防止把整棵 VOM 树塞进上下文。"""

DEFAULT_ENABLE_EVALUATE = False
"""是否允许在页面里执行任意 JavaScript（``bsk evaluate``）。默认关闭。

这是本插件风险最高的能力，所以不复用 ``enabled`` 总开关，而是单独一项：

- 它能在用户已登录的页面里跑任意脚本：读邮箱内容、读后台数据、读 token；
- 它能静默提交表单、带 cookie 发请求，绕开"用户看得见的动作"这层约束
  —— click/fill 至少会在浏览器窗口里显示出来，执行 JS 不会；
- 它能自动确认 ``confirm`` 弹窗（实测确认），于是"页面自己会拦一下"的
  最后一道人工确认也失效了。

默认关闭意味着升级用户不会因为装了新版本就凭空多出一个高危能力。
"""

DEFAULT_EVALUATE_REQUIRE_ADMIN = True
"""执行 JavaScript 是否强制仅 AstrBot 管理员可用。默认开启且建议保持。

这一项与 ``admin_only`` 是独立的，而且优先级更高：即使把 ``admin_only``
关掉（或把调用者写进了 ``allowed_users`` 白名单），只要本项为 True，非管理员
依然无法执行 JS。

为什么必须让它盖过 ``admin_only``：``admin_only=False`` 的语义是"我愿意把
'看得见的浏览器操作'开放给其他人"，而执行任意 JS 是完全不同量级的授权
（它能静默读数据、静默发请求）。让一个宽松的粗粒度开关顺手把最高危能力一起
放开，是最容易被忽略的权限放大路径。要开放就得显式再关一项。
"""

DEFAULT_FULLPAGE_TIMEOUT_SEC = 120.0
"""全页截图「最多等多久」（秒）—— 用户可调，默认 120。

这个默认值是按实测数据定的，不是拍脑袋：本机实测长页面（Wikipedia 长条目）
全页截图 11.72 / 11.11 / 10.91 秒（1820x11741，4.5MB），短页面（example.com）
全页截图 2.91 秒、视口截图 0.12 秒。120 秒对实测的 11.72 秒有约 10 倍余量，
把"模型/页面慢"这类正常波动都装得下。

它替代了此前硬编码的 180 秒常量（``service.TIMEOUT_FULLPAGE`` 现在只是这一项的
默认值来源）。改为可调是因为"浏览器操作最多等多久"是使用者的判断，不该由插件
替他定死；上限给到 600 秒，让放宽了框架 ``tool_call_timeout`` 的用户能真的用上
更大的值。

⚠️ 它管的是"调用浏览器的这一段最多等多久"，与模型生成回复的快慢无关。
AstrBot 的 ``tool_call_timeout`` 限制的也是工具执行耗时，不包含模型出 token
的时间。所以"模型很慢"不是调大它的理由 —— 页面特别慢、网络特别差才是。
"""

# --------------------------------------------------------------------------
# 取值边界与外部约束
# --------------------------------------------------------------------------

ASTRBOT_TOOL_TIMEOUT_LIMIT_SEC = 120.0
"""AstrBot 单次工具调用的时间上限（秒）—— 默认值，不是实测值。

来源：``core/agent/run_context.py:19``（``tool_call_timeout: int = 120``）、
``core/config/agent_runner.py:33``（默认配置同值）；用户可改，改的是
``agent_runner.config.misc.tool_call_timeout``（本机实测该配置项确实是 120）。

到点后框架会杀掉本次工具调用并抛 ``tool <name> execution timeout after N seconds.``
（``astr_agent_tool_exec.py:691-726``：``asyncio.wait_for(anext(wrapper), timeout=...)``），
用户看到的就是这句英文 —— 而不是我们写的中文提示。

⚠️ 这个默认值曾把我引到一个错误的结论上：既然框架默认只等 120 秒，而全页截图
当时的内置下限是 180 秒，就推断"全页截图必须两处一起调大才能用"。实测推翻了它：
真实长页面（Wikipedia 长条目）的全页截图只要 11.72 / 11.11 / 10.91 秒
（1820x11741、4.5MB），是 120 秒限制的约 1/10，留了 108 秒余量；短页面
（example.com）视口截图 0.12 秒、全页截图 2.91 秒。所以"必须调大"是个多余的警告。
（该说法已从 README/ARCHITECTURE 删除；全页截图的下限后来也按用户要求从 180 改成
可配置的 120，见 :data:`DEFAULT_FULLPAGE_TIMEOUT_SEC`。）

真正的风险是另一个（也是这个常量仍然有用的原因）：它一旦小于我们的最终超时，
用户看到的就会是框架抛的英文超时，而不是插件精心写的中文提示，会话还可能
留下未完成状态。所以运行时要把这个值读出来并给自己的超时留出安全余量 ——
见 :func:`read_framework_tool_timeout` 与 ``service.BskService._timeout``。
"""

# --------------------------------------------------------------------------
# 框架侧超时：读取与钳制
#
# 背景：本插件的最终超时曾经只由"内置下限 vs 用户配置"决定，于是全页截图会拿到
# 180 秒。但框架默认只等 120 秒（且用户可改），结果就是插件还没到点、框架先把
# 这次调用掐了 —— 用户看到的是一句英文的 execution timeout，我们精心写的中文
# 提示根本没机会出现，而且"会话可能留下未完成状态"这件事被完全掩盖。
#
# 解法：把框架的上限读出来，让自己的超时始终低于它。读不到就一切照旧
#（宁可用原语义，也不要凭猜测缩短用户的等待）。
# --------------------------------------------------------------------------

FRAMEWORK_TOOL_TIMEOUT_PATH: tuple[str, ...] = (
    "agent_runner",
    "config",
    "misc",
    "tool_call_timeout",
)
"""框架 ``tool_call_timeout`` 在 AstrBot 主配置里的路径。

已用真实配置文件（``~/.astrbot/data/cmd_config.json``）确认过这条路径与结构，
本机取到的值 = 120。它同时也是一个拼错的提示该照抄的路径：
用户要改的就是这一项。
"""

FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC = 5.0
"""最终超时必须留在框架上限之下的安全余量（秒）。

为什么需要它（而不是取"正好等于上限"）：框架的计时是从它开始等算起的
（``asyncio.wait_for`` 包住整条工具调用），而一次工具调用不只是 bsk 子进程本身 ——
bsk 返回之后我们还要校验截图、渲染给模型的中文、序列化结果、把图片交给框架发送。
这些收尾都在同一个 120 秒窗口里。若插件超时正好等于上限，就会出现"bsk 刚好返回、
框架同时掐断"的临界情况：用户拿不到任何中文解释。

5 秒的依据：收尾动作实测都在毫秒级，5 秒是宽裕的余量，同时小到不会实质缩短
用户愿意等待的时间。它只是一个余量，不承担"压低命令超时"的职责。
"""

FRAMEWORK_TIMEOUT_FLOOR_SEC = 10.0
"""钳制后的最终超时不得低于这个值（秒）。

没有它的话，用户把 ``tool_call_timeout`` 调到 120 以上是小概率事件，但把它调到
15 秒却完全可能 —— 那时按公式会算出 15 - 5 = 10 秒甚至更小。超时太小比超时更糟：
每条命令都会在起点就被掐死（连 `observe` 都来不及完成），现象是"插件完全不能用"，
比"慢一点但能成"难排查得多。所以宁可让它最多少等 10 秒。
"""


def as_timeout_seconds(value: Any) -> float | None:
    """把外部配置里的超时值收敛成正的浮点秒数，拿不准时返回 ``None``。

    这是 :func:`read_framework_tool_timeout` 的取值归一化步骤，语义与
    ``_as_float`` 一致：接受数字与"看起来像数字的字符串"，其余一律 ``None``。

    Args:
        value: 任意原始值（本地配置文件是 JSON，值类型不可信）。

    Returns:
        正的浮点秒数；布尔、非数字、``nan`` / ``inf``、0 与负数都返回 ``None``。
    """
    if isinstance(value, bool):
        # True/False 当超时没有意义（True 会被 isinstance(int) 放过）。
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except (ValueError, AttributeError):
            return None
    if not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):  # pragma: no cover - 防御性
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def read_framework_tool_timeout(config_obj: Any) -> float | None:
    """从 AstrBot 的主配置对象里读出框架的工具调用超时（秒）。

    本函数只接受"鸭子类型"对象，绝不 import astrbot。 分层约束要求 ``bsk/``
    包零框架依赖（有测试与 ``verify_release_ready.py`` 守护），所以这里靠
    ``getattr`` / ``.get()`` 逐层小心下探，调用方（``main.py``）负责把
    ``self.context.get_config()`` 的返回值传进来。

    取值路径：``agent_runner`` → ``config`` → ``misc`` → ``tool_call_timeout``
    （见 :data:`FRAMEWORK_TOOL_TIMEOUT_PATH`）。

    任何异常都降级成"未知"（返回 ``None``），绝不抛异常。 这个函数跑在插件
    加载路径上：它一抛，整个插件就加载失败 —— 而"框架上限读不到"最多只是让我们
    少一层保护，绝不该让插件起不来。这与 :func:`parse_settings` 的"非法值一律回退"
    是同一个约定。

    具体的降级情形（都有测试覆盖）：

    - 传进来是 ``None``、空 dict、字符串、对象等任何"不是配置"的东西；
    - 路径上任何一层缺失（旧版 AstrBot 没有 ``agent_runner``）；
    - 某一层存在但取值是 ``None`` / 字符串 / 列表，无法继续下探；
    - 叶子值是字符串（``"120"`` 可用）、``0``、负数、``nan`` / ``inf``、布尔。

    Args:
        config_obj: AstrBot 的主配置对象。真实形态是 ``AstrBotConfig``
            （``dict`` 的子类，且支持 ``.键名`` 属性访问），这里对两种访问方式
            都做尝试，也对普通 dict 生效。

    Returns:
        读到且合法时返回正的浮点秒数；读不到或值不可用时返回 ``None``
        （调用方据此保持原有行为，不要因为读不到就缩短超时）。
    """
    try:
        node: Any = config_obj
        for key in FRAMEWORK_TOOL_TIMEOUT_PATH:
            node = _dig_one_level(node, key)
            if node is None:
                return None
        return as_timeout_seconds(node)
    except Exception:  # noqa: BLE001 - 读不到就是"未知"，绝不能影响插件加载
        return None


def _dig_one_level(node: Any, key: str) -> Any:
    """从配置树里取一层子节点，取不到时返回 ``None``。

    ``AstrBotConfig`` 既是 dict 又支持 ``.键名`` 属性访问，所以两种方式都试。
    刻意不用 ``isinstance(node, dict)`` 去卡类型：真实的 ``AstrBotConfig``
    恰好是 dict 子类，但将来若换成别的映射类型，用鸭子类型（有没有 ``get``）
    判断更稳。任何异常都吞掉并返回 ``None``。
    """
    getter = getattr(node, "get", None)
    if callable(getter):
        try:
            found = getter(key)
        except Exception:  # noqa: BLE001 - 畸形映射类型不该让我们崩
            found = None
        if found is not None:
            return found
    try:
        return getattr(node, key, None)
    except Exception:  # noqa: BLE001
        return None


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

在新语义（最终超时 = ``max(内置下限, 本项)``）下这个上界仍然合理：
它必须严格小于 ``ASTRBOT_TOOL_TIMEOUT_LIMIT_SEC``（120），这样即便用户把它
拉满，插件内部也总有时间把超时翻译成中文提示，而不是被框架从外面打断。
"""

IDLE_RELEASE_MIN_SEC = 60.0
IDLE_RELEASE_MAX_SEC = 299.0
"""空闲释放的夹取区间。上界必须严格小于 ``BSK_SESSION_RECLAIM_SEC``。"""

FULLPAGE_TIMEOUT_MIN_SEC = 30.0
FULLPAGE_TIMEOUT_MAX_SEC = 600.0
"""全页截图超时的夹取区间（秒）。

下界 30 的依据：实测最慢一次全页截图是 11.72 秒，30 秒约为它的 2.5 倍，
再低就等于"必然超时"，填了也没有意义。

上界 600 的依据：它必须允许超过框架默认的 120 秒，否则用户放宽了框架的
``tool_call_timeout``（例如调到 300）之后，插件这边仍然卡在 120 用不上更大的值。
600 秒足够覆盖任何真实网页，同时仍是个有界的值（不至于等价于"永不超时"）。
"""

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
# 全部遵循同一个约定：拿不准就返回 default，绝不抛异常。
# --------------------------------------------------------------------------

# 字符串形式的布尔值。用户把开关填成字符串是很常见的（配置文件手改、
# 从别处复制粘贴），所以必须认这些写法。
_TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on", "y", "t", "是", "开", "启用"})
_FALSY_STRINGS = frozenset({"0", "false", "no", "off", "n", "f", "否", "关", "禁用"})
# 注意空字符串不在上面两个集合里：``enabled: ""`` 说明用户没填（或填错了），
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
    """插件运行所需的全部配置，保证每一项都已合法。

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
    """所有 bsk 命令的超时（秒）。

    最终超时 = ``max(该命令的内置下限建议值, 本项)``，见 ``service.BskService._timeout``。
    """

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

    fullpage_timeout_sec: float
    """整页截图（``--full-page``）最多等多久（秒），默认 120，夹取到 ``[30, 600]``。

    只作用于全页截图那一条命令；视口截图仍然是 ``service.TIMEOUT_SCREENSHOT``（30 秒）。
    最终值还会被框架上限钳一次（见 ``service.BskService._timeout``）。
    详见 :data:`DEFAULT_FULLPAGE_TIMEOUT_SEC`。
    """

    screenshot_dir: str
    """截图存放目录。空字符串 = 插件数据目录下的 ``shots``。"""

    journal_path: str
    """会话 journal 文件位置。空字符串 = 插件数据目录下的 ``sessions.json``。

    journal 是"我创建了哪些浏览器会话"的落盘记录，用于 AstrBot 被强杀后
    在下次启动时回收遗留的会话（见 ``bsk/journal.py`` 与
    ``SessionManager.recover_orphans``）。
    """

    data_dir: str
    """插件数据目录（``data/plugin_data/astrbot_plugin_bsk_browser``）。

    **注入项，不是用户配置项**：由 ``main.py`` 通过 ``StarTools.get_data_dir()``
    取得后传给 :func:`parse_settings`，所以它刻意不写进 ``_conf_schema.json``
    —— 用户不该也无法在配置页里填它。

    空串表示"未注入"（拿不到数据目录），此时 journal 与截图的默认位置会降级到
    系统临时目录（见 ``bsk/paths.py``）。它只在 ``screenshot_dir`` /
    ``journal_path`` **为空**时才有影响：用户显式填了的那两项优先级最高。
    """

    max_page_chars: int
    """回给模型的页面文本上限（字符数）。"""

    enable_evaluate: bool
    """是否允许执行任意 JavaScript（``bsk evaluate``）。默认 False。

    与 ``enabled`` 无关：``enabled`` 是"插件是否工作"，本项是"是否额外开放
    最高危的那个能力"。详见 :data:`DEFAULT_ENABLE_EVALUATE`。
    """

    evaluate_require_admin: bool
    """执行 JavaScript 是否强制仅管理员。默认 True，且优先于 ``admin_only``。

    为 True 时，``admin_only=False`` 与 ``allowed_users`` 都不能让非管理员
    执行 JS —— 详见 :data:`DEFAULT_EVALUATE_REQUIRE_ADMIN`。
    """


def parse_settings(raw: dict | None, *, data_dir: str = "") -> Settings:
    """从 AstrBot 的原始配置 dict 构造 :class:`Settings`。

    任何非法值都回退到默认值，任何越界值都夹到边界，本函数永不抛异常。
    用户把配置填成一团乱麻的后果是"插件按默认值工作"，而不是"插件起不来"。

    Args:
        raw: AstrBot 传来的原始配置。``None``、空 dict、甚至根本不是 dict
            都会被当成"没有配置"处理，全部走默认值。
        data_dir: 插件数据目录，由 ``main.py`` 注入（见 :attr:`Settings.data_dir`）。
            它是关键字参数，因为**它不是用户配置**：调用方要么显式传，
            要么就接受"未注入"这个默认状态。默认空串照原样存下，
            不做任何解析或校验 —— 路径的可用性判断与降级全部由
            ``bsk/paths.py`` 在真正要用的时候做（那时才知道写不写得进去，
            而且那一步保证不抛异常）。

    Returns:
        校验后的配置对象。

    Note:
        用户显式配置的 ``screenshot_dir`` / ``journal_path`` **优先级最高**：
        ``data_dir`` 只在它们为空时才决定默认落点，绝不会覆盖它们。
    """
    if not isinstance(raw, dict):
        # AstrBot 正常会传 dict（AstrBotConfig 是 dict 子类），这里只是兜底。
        raw = {}

    screenshot_dir = raw.get("screenshot_dir")
    journal_path = raw.get("journal_path")
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
        # 整页截图专用超时。上界（600）刻意大于框架默认的 120 秒：
        # 用户放宽了框架 tool_call_timeout 之后，这一项要能真的用上更大的值；
        # 是否被框架上限钳住由 service._timeout 在运行时决定（那里才读得到框架值）。
        fullpage_timeout_sec=_clamp(
            _as_float(
                raw.get("fullpage_timeout_sec"), DEFAULT_FULLPAGE_TIMEOUT_SEC
            ),
            FULLPAGE_TIMEOUT_MIN_SEC,
            FULLPAGE_TIMEOUT_MAX_SEC,
        ),
        # 空字符串在这里是有意义的取值（表示"用默认目录"），所以不能让
        # _as_str 把它折成默认值 —— 它的默认值本来也就是空字符串，正好一致。
        screenshot_dir=_as_str(screenshot_dir, DEFAULT_SCREENSHOT_DIR),
        # 同上：空 = 用插件数据目录下的默认位置，与 screenshot_dir 一个套路。
        journal_path=_as_str(journal_path, DEFAULT_JOURNAL_PATH),
        # 注入项（不是用户配置项）：原样收下，不做解析也不做存在性检查。
        # 路径能不能用、不能用时降级到哪里，全部交给 bsk/paths.py 在使用时判定
        # —— 那时才知道写不写得进去，而且那一步保证不抛异常。
        data_dir=_as_str(data_dir, ""),
        max_page_chars=int(
            _clamp(
                _as_int(raw.get("max_page_chars"), DEFAULT_MAX_PAGE_CHARS),
                MAX_PAGE_CHARS_MIN,
                MAX_PAGE_CHARS_MAX,
            )
        ),
        # 默认 False 意味着"用户没填这一项"与"用户明确关掉"是同一个结果 ——
        # 这正是我们要的：新增高危能力不该因为配置缺失而被打开。
        enable_evaluate=_as_bool(raw.get("enable_evaluate"), DEFAULT_ENABLE_EVALUATE),
        evaluate_require_admin=_as_bool(
            raw.get("evaluate_require_admin"), DEFAULT_EVALUATE_REQUIRE_ADMIN
        ),
    )


def validate_settings(s: Settings) -> list[str]:
    """检查配置里"合法但会出问题"的地方，返回给用户看的中文提示。

    只报告实质问题，不报告非法值 —— 非法值已经在 :func:`parse_settings`
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
            f"的 {ASTRBOT_TOOL_TIMEOUT_LIMIT_SEC:g} 秒上限。这一项是所有 bsk 命令的"
            "统一超时，值得再大也没用：AstrBot 会在命令返回之前就掐断这次调用，"
            "机器人只会看到一句失败，而浏览器那边可能还在动。"
            f"建议改到 {DEFAULT_COMMAND_TIMEOUT_SEC:g} 秒左右"
            "（插件最多只接受 110 秒）。注意 AstrBot 这一项默认 120 秒，而且它才是"
            "真正的天花板：插件会把自己的超时钳到它之下（留 5 秒余量），"
            "所以你把本项调得再大也不会超过框架允许的时间。"
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
            "【安全提醒】`admin_only` 已经关闭：任何能给机器人发消息的人都能操控"
            "你这台机器上已登录的浏览器 —— 读你的网页、点按钮、填表单、截图。"
            "如果只是想给某个人用，请把 `admin_only` 改回开启，并把对方的用户 ID"
            "填进 `allowed_users`。"
        )

    # evaluate 是本插件风险最高的能力，它的提醒必须单独一条，且说清
    #   "它能做什么"而不是"它被打开了" —— 用户要判断的是风险，不是开关状态。
    if _as_bool(getattr(s, "enable_evaluate", False), False):
        problems.append(
            "【安全提醒】`enable_evaluate` 已开启：模型现在可以在你已登录的页面里"
            "执行任意 JavaScript。这与点击、输入有本质区别 —— 后者是你在浏览器窗口里"
            "看得见的动作，而执行脚本不是："
            "它能静默读取页面上的任何数据（邮箱正文、后台列表、登录 token），"
            "能带着你的登录状态发请求或提交表单，还能自动点掉页面的确认弹窗"
            "（实测 `confirm` 会被自动确认为「确定」）。"
            "如果只是想让它读页面文字，`enable_evaluate` 应保持关闭；"
            "确实需要时，请同时确认 `evaluate_require_admin` 是开启的"
            "（默认开启），并且清楚这个机器人会被哪些人使用。"
        )

    if _as_bool(getattr(s, "enable_evaluate", False), False) and not _as_bool(
        getattr(s, "evaluate_require_admin", True), True
    ):
        problems.append(
            "【安全提醒】`enable_evaluate` 与「执行脚本仅限管理员」是同时关闭的："
            "现在任何能跟机器人对话的人（包括 `allowed_users` 白名单里的人，以及"
            "`admin_only=false` 时所有能发消息的人）都可以让你的浏览器执行任意 "
            "JavaScript，读取或提交你已登录账号里的任何内容。"
            "除非你完全清楚后果，否则请把 `evaluate_require_admin` 改回开启。"
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
            f"`allowed_users` 里填了 {len(users)} 个用户（{shown}），同时 `admin_only` "
            "也是开启的。实际规则是白名单优先：名单里的用户会被放行，"
            "名单外的用户即使是管理员之外的普通人也会被挡住；而 AstrBot 管理员"
            "始终可用。也就是说，这份白名单会让名单里的非管理员也能操作浏览器。"
            "如果不希望这样，请清空 `allowed_users`。"
        )

    return problems
