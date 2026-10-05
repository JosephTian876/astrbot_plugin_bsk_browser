"""落盘位置的统一解析 —— 插件数据目录，以及拿不到时的降级。

会话所有权 journal 与截图这两处"要写到磁盘上"的东西都从这里取位置，原因是
它们必须遵守同一条规则（审核要求）：**持久化数据存到插件数据目录**
``data/plugin_data/astrbot_plugin_bsk_browser`` 下。

但这条规则不能写成硬依赖：数据目录由 ``main.py`` 通过
``StarTools.get_data_dir()`` 取得，而那一步**失败时会抛异常**。本模块在插件
加载路径上跑，抛异常等于插件起不来 —— 这是绝对不能接受的失败模式。所以：

1. ``data_dir`` 非空、可创建、可写 → 用它（正常路径）；
2. 否则 → 退回系统临时目录下的 ``astrbot_bsk_browser``，并记一条 warning；
3. 任何一步都不抛异常。

降级只影响数据的存放位置（临时目录会被操作系统清理），不影响功能。它**不能
删掉**：journal 与截图都必须有个可写的落点，"数据目录拿不到"不该升级成
"插件不可用"，这也是审核原文明确要求保留的容错。

本模块零 astrbot 依赖、零第三方依赖，可以脱离框架直接单测。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .logger import NULL_LOGGER

__all__ = [
    "JOURNAL_FILE_NAME",
    "SHOT_DIR_NAME",
    "TEMP_DIR_NAME",
    "default_journal_path",
    "default_shot_dir",
    "resolve_data_dir",
]

TEMP_DIR_NAME = "astrbot_bsk_browser"
"""降级目录名（系统临时目录下）。journal 与截图共用它，便于人工排查。"""

JOURNAL_FILE_NAME = "sessions.json"
"""journal 文件名。用 JSON 而不是二进制，是为了出问题时能直接用记事本打开看。"""

SHOT_DIR_NAME = "shots"
"""截图子目录名（数据目录下）。刻意与 journal 分开，便于人工排查。"""


def _as_path(data_dir: Any) -> Path | None:
    """把外部传入的 ``data_dir`` 收敛成 ``Path``，不是路径就返回 ``None``。

    只接受字符串与 ``os.PathLike``：数字、布尔、bytes、列表、字典、任意对象
    都说明调用方传错了，这时走降级比 ``Path(...)`` 抛 ``TypeError`` 正确 ——
    本模块在插件加载路径上。
    """
    if not isinstance(data_dir, (str, os.PathLike)):
        return None
    try:
        text = str(data_dir).strip()
    except Exception:  # noqa: BLE001 - 畸形的 PathLike 也可能在 __str__ 上炸
        return None
    if not text:
        return None
    try:
        return Path(text)
    except Exception:  # noqa: BLE001 - 含空字节等非法路径
        return None


def _usable_data_dir(data_dir: Any) -> Path | None:
    """``data_dir`` 可创建可写时返回它的绝对路径，否则返回 ``None``。

    "可写"用两步确认：能建出来（父级不可写、父路径是个文件、名字是 Windows
    保留设备名都会在这里失败），以及 ``os.access`` 认为可写。
    """
    candidate = _as_path(data_dir)
    if candidate is None:
        return None
    try:
        if not candidate.is_absolute():
            # 相对路径会随工作目录漂移，而 journal 要跨进程读 —— 钉成绝对路径。
            candidate = Path.cwd() / candidate
        candidate = candidate.resolve()
    except Exception:  # noqa: BLE001 - 非法路径（空字节、保留设备名等）
        return None
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 - 建不出来就是不可用
        return None
    try:
        if not os.access(candidate, os.W_OK):
            return None
    except Exception:  # noqa: BLE001 - 极端环境下 access 也可能炸
        return None
    return candidate


def _temp_root() -> Path:
    """降级用的根目录：``<系统临时目录>/astrbot_bsk_browser``（绝对路径）。

    ``gettempdir()`` 本身在极端环境下也会抛，所以兜一层"当前目录"再 resolve
    成绝对路径 —— 降级路径返回相对路径的话，跨进程读 journal 会读错地方。
    """
    try:
        base = Path(tempfile.gettempdir())
    except Exception:  # noqa: BLE001 - 极端环境下 gettempdir 也可能炸
        base = Path(".")
    root = base / TEMP_DIR_NAME
    try:
        return root.resolve()
    except Exception:  # noqa: BLE001
        return root


def _warn(logger: Any, message: str, *args: Any) -> None:
    """记一条降级 warning；logger 不可用或本身有问题都不能影响主流程。"""
    try:
        (logger or NULL_LOGGER).warning(message, *args)
    except Exception:  # noqa: BLE001 - 日志失败绝不升级成功能失败
        pass


def resolve_data_dir(data_dir: Any, logger: Any = None) -> Path:
    """解析出实际使用的数据目录，拿不到就降级到系统临时目录。

    Args:
        data_dir: ``main.py`` 注入的插件数据目录（``Settings.data_dir``）。
            空串表示"未注入"，``None`` / 非字符串 / 非法路径同样按未注入处理。
        logger: 可选。降级时用它记一条 warning，不传则静默降级。

    Returns:
        **总是**返回一个绝对路径：可用时是 ``data_dir`` 本身（已确保存在），
        否则是 ``<系统临时目录>/astrbot_bsk_browser``。绝不返回 ``None`` ——
        调用方需要的是一个能直接用的落点，而不是一个还要再判空的返回值。

    Note:
        本函数不抛异常。它会把目录建出来，所以调用它意味着接受一次 mkdir。
    """
    resolved = _usable_data_dir(data_dir)
    if resolved is not None:
        return resolved
    fallback = _temp_root()
    _warn(
        logger,
        "插件数据目录不可用（%r），已降级到系统临时目录 %s；"
        "该目录可能被操作系统清理，journal 与截图会在那里丢失。",
        data_dir,
        fallback,
    )
    return fallback


def default_journal_path(data_dir: Any = "", logger: Any = None) -> Path:
    """journal 文件的默认位置：``<数据目录>/sessions.json``。

    数据目录不可用时降级到 ``<系统临时目录>/astrbot_bsk_browser/sessions.json``
    （迁移之前的行为）。

    Args:
        data_dir: 插件数据目录（见 :func:`resolve_data_dir`）。
        logger: 可选，仅用于降级告警。

    Returns:
        journal 文件的绝对路径（不保证文件存在，父目录会被解析步骤建出来）。
    """
    return resolve_data_dir(data_dir, logger) / JOURNAL_FILE_NAME


def default_shot_dir(data_dir: Any = "", logger: Any = None) -> Path:
    """截图的默认根目录：``<数据目录>/shots``。

    数据目录不可用时降级到 ``<系统临时目录>/astrbot_bsk_browser/shots``。

    Note:
        这里返回的是"截图根目录"，真正的文件还会再往下两层
        （``<本目录>/shots/<session_id>/<文件名>``，见
        ``bsk.shots.make_shot_path``）—— 与迁移之前的结构逐字相同，改的只是
        根目录落在哪里。

    Args:
        data_dir: 插件数据目录（见 :func:`resolve_data_dir`）。
        logger: 可选，仅用于降级告警。

    Returns:
        截图根目录的绝对路径（不保证目录存在）。
    """
    return resolve_data_dir(data_dir, logger) / SHOT_DIR_NAME
