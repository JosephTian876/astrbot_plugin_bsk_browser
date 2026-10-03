"""L4 工具层端到端测试：直接 await 插件注册给 LLM 的 **6 个工具函数**。

## 这一层补的是什么缺口

``main.py`` 里的每个 ``@filter.llm_tool`` 函数内部都有自己的一段逻辑：
参数校验、权限门、URL 协议白名单、异常包装、``bsk_screenshot`` 的 async generator
行为、``bsk_act`` 的动作名归一化。既有的三层测试都不覆盖它：

- **L1**（``tests/test_*.py``）只测 ``bsk/`` 纯逻辑，用假 runner；
- **L2**（``tests/verify_astrbot_contract.py``）只证明 6 个工具**注册成功**、
  docstring schema 正确、生命周期方法可调用，**没有调用过工具函数本身**；
- **L3**（``tests/verify_integration.py``）测的是 ``BskService`` 服务层，
  **不经过 main.py 的工具函数**。

所以本脚本的定位是 L2 与 L3 之间缺掉的那一层：在**真实 AstrBot 环境**里加载插件、
拿到插件实例，把工具函数当普通方法直接 await，用**真实浏览器**验证行为。

## 加载方式与一致性检查

工具函数被框架用 ``handler.__module__ == metadata.module_path`` 判定，所以必须走
``__import__("data.plugins.<目录>.main", fromlist=["main"])`` 这条真实加载路径。
注意：**被测代码是安装目录那份**，而源码目录是我们编辑的那份。如果两者不一致，
测试会在旧代码上通过而给出假信心 —— 所以脚本开头用 SHA256 严格比对
``main.py`` 与 ``bsk/*.py``，不一致就**直接失败并提示同步**，绝不静默继续。

## 安全边界（与 L3 一致，不得放宽）

- 只访问 ``https://example.com``，以及第 6 组用例里那个按 RFC 6761 保留、
  永不解析的 ``.invalid`` 域名；
- 只用插件自己开的 Agent Window，**绝不借用（borrow）用户的标签页**；
- 不做 click / fill / press / upload / download / evaluate（``bsk_act`` 只做只读的
  ``scroll_to``）；
- **绝不使用 ``bsk session stop --all``**，只按精确 session id 停；
- 结束时关闭**本测试创建的所有会话**；断言只比对"本测试创建的 session id 是否还在
  daemon 里"，**不能**断言 daemon 会话数为 0（那会把用户自己的 DSH 会话也算进来而误报）。

用法：
    python tests/verify_tools_e2e.py

退出码：全部通过 0，有任何失败 1。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent

PLUGIN_DIR_NAME = "astrbot_plugin_bsk_browser"
PLUGIN_PKG = f"data.plugins.{PLUGIN_DIR_NAME}"
MODULE_NAME = f"{PLUGIN_PKG}.main"

# AstrBot 实际 import 的那个目录（被测代码的真正来源）。
INSTALLED = Path(
    os.environ.get(
        "BSK_PLUGIN_INSTALLED",
        str(Path(os.path.expanduser("~")) / ".astrbot" / "data" / "plugins" / PLUGIN_DIR_NAME),
    )
)

# --- 测试用的固定输入 ---------------------------------------------------------
# umo 全部带 e2e 前缀，避免和用户真实的会话键撞在一起。
URL_OK = "https://example.com"
URL_DEAD = "https://this-domain-definitely-does-not-exist-xyz.invalid"
UMO_BROWSER = "e2e-tools:umo:browser"
UMO_NONADMIN = "e2e-tools:umo:nonadmin"
UMO_ISO_B = "e2e-tools:umo:iso-b"
UMO_ISO_C = "e2e-tools:umo:iso-c"
UMO_DEAD = "e2e-tools:umo:dead"
UMO_BROKEN_IMG = "e2e-tools:umo:brokenimg"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 异常堆栈的指纹：工具返回值里出现这些，说明异常穿到了框架（用户在聊天里会看到堆栈）。
STACK_MARKERS = ("Traceback (most recent call last)", 'File "', "raise ", "Exception:")

# --- 全局状态（在 main() / run() 里初始化）------------------------------------
MODULE = None
FakeEvent = None
make_context = None
BskRunner = None
BSK_PATH = ""
SHOT_DIR = ""

RESULTS: list[tuple[str, bool, str]] = []
"""全部用例：(名称, 是否通过, 细节)。"""

INSTANCES: list[object] = []
"""本测试创建过的所有插件实例，清理时逐个 terminate。"""

RUNNER = None
"""独立于插件的 runner，只用来问 daemon "现在有哪些会话"。"""

CREATED_SESSION_IDS: set[str] = set()
"""本测试创建过的 bsk 会话 id（清理断言只比对这一集合）。"""


# ---------------------------------------------------------------------------
# 输出与记录
# ---------------------------------------------------------------------------


def record(name: str, ok: bool, detail: str = "") -> bool:
    """记录一条用例并立即打印。返回是否通过，便于调用方串联判断。"""
    RESULTS.append((name, ok, detail))
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if detail else ""))
    return ok


def note(text: str) -> None:
    """打印一条不计入用例的补充信息（缩进对齐）。"""
    print(f"       {text}")


def banner(text: str) -> None:
    print()
    print(f"--- {text} " + "-" * max(0, 66 - len(text)))


# ---------------------------------------------------------------------------
# 一致性检查：源码目录 vs 安装目录
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _python_files(root: Path) -> dict[str, str]:
    """收集参与比对的源文件：``main.py`` 与 ``bsk/*.py``。"""
    files: dict[str, str] = {}
    main_py = root / "main.py"
    if main_py.is_file():
        files["main.py"] = _sha256(main_py)
    bsk_dir = root / "bsk"
    if bsk_dir.is_dir():
        for item in sorted(bsk_dir.glob("*.py")):
            files[f"bsk/{item.name}"] = _sha256(item)
    return files


def consistency_check() -> bool:
    """比对源码目录与安装目录的 ``main.py`` / ``bsk/*.py`` 内容。

    为什么必须做：本脚本的 **import 走安装目录**，而人（和 CI）改的是**源码目录**。
    两者不一致时测试会在旧代码上"通过"，是最危险的一种假阳性。这里用文件哈希
    严格比对，不一致就报错并提示同步方式，而不是继续跑。

    Returns:
        True 表示两边完全一致，可以继续。
    """
    print("=" * 72)
    print("L4 工具层端到端测试：直接 await 6 个 @filter.llm_tool 工具函数")
    print("=" * 72)
    print(f"源码目录：{PROJECT}")
    print(f"安装目录：{INSTALLED}")

    if not INSTALLED.is_dir():
        record("源码目录与安装目录一致", False, f"安装目录不存在：{INSTALLED}")
        return False

    src = _python_files(PROJECT)
    dst = _python_files(INSTALLED)

    missing = sorted(set(src) - set(dst))
    extra = sorted(set(dst) - set(src))
    differing = sorted(k for k in set(src) & set(dst) if src[k] != dst[k])

    if missing or extra or differing:
        detail_parts = []
        if differing:
            detail_parts.append(f"内容不同：{differing}")
        if missing:
            detail_parts.append(f"安装目录缺文件：{missing}")
        if extra:
            detail_parts.append(f"安装目录多文件：{extra}")
        record("源码目录与安装目录一致", False, "；".join(detail_parts))
        print()
        print("！被测代码（安装目录）与源码目录不一致，测试结果不能代表源码。")
        print("  请先同步后再跑，例如：")
        print(
            f'  robocopy "{PROJECT}" "{INSTALLED}" /MIR /XD .git __pycache__ .pytest_cache'
        )
        return False

    record("源码目录与安装目录一致", True, f"{len(src)} 个文件哈希全部相同")
    return True


# ---------------------------------------------------------------------------
# 路径与导入
# ---------------------------------------------------------------------------


def _ensure_paths() -> None:
    """把必要的路径放进 ``sys.path``，让脚本可以独立运行（不依赖外部 PYTHONPATH）。

    - 本测试目录：``import astrbot_test_doubles``
    - 插件仓库根：``import bsk``（只在需要读常量时用）
    - AstrBot 应用目录：``import astrbot``
    - ``~/.astrbot``：``import data.plugins.<插件>``（AstrBot 真实加载路径）
    """
    for path in (str(HERE), str(PROJECT)):
        if path not in sys.path:
            sys.path.insert(0, path)

    astrbot_app = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
    if os.path.isdir(astrbot_app) and astrbot_app not in sys.path:
        sys.path.insert(0, astrbot_app)

    astrbot_root = os.environ.get(
        "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
    )
    if os.path.isdir(astrbot_root) and astrbot_root not in sys.path:
        sys.path.insert(0, astrbot_root)


def _resolve_bsk_path() -> str:
    """定位 bsk 可执行文件（优先环境变量，其次明文绝对路径，最后交给 PATH）。"""
    from_env = os.environ.get("BSK_PATH", "").strip()
    if from_env:
        return from_env
    guess = Path(os.path.expanduser("~")) / ".local" / "bin" / "bsk.exe"
    if guess.is_file():
        return str(guess)
    return "bsk"


def _installed_submodule(name: str):
    """取**安装目录**那份代码里的子模块（保证用被测代码本身做辅助操作）。"""
    return sys.modules[f"{PLUGIN_PKG}.bsk.{name}"]


# ---------------------------------------------------------------------------
# 调用工具函数的小工具
# ---------------------------------------------------------------------------


async def call(fn, *args, **kwargs):
    """await 一个工具函数，返回 ``(结果, 异常)``。

    刻意**不**让异常冒出来：本层要验证的核心之一就是"工具函数绝不把异常抛给框架"，
    所以异常必须被捕捉成数据交给用例去判定，而不是中断整个脚本。
    """
    try:
        return await fn(*args, **kwargs), None
    except BaseException as exc:  # noqa: BLE001 - 异常本身就是被测对象
        return None, exc


async def consume(gen) -> tuple[list, BaseException | None]:
    """消费一个 async generator，返回 ``(已 yield 的项, 异常)``。

    ``bsk_screenshot`` 是 async generator（框架的 ``call_local_llm_tool`` 就是
    用 ``async for`` 消费它的），不能直接 await。
    """
    items: list = []
    try:
        async for item in gen:
            items.append(item)
        return items, None
    except BaseException as exc:  # noqa: BLE001
        return items, exc


def check_str_result(name: str, value, exc, must_contain: tuple[str, ...] = ()) -> bool:
    """通用断言：不抛异常 + 返回 str + 含指定片段。

    这三条正是"工具函数把错误转成模型能看懂的中文"的最低要求 ——
    任何一条不满足，用户就会在聊天里看到堆栈或收到 ``None``。
    """
    if exc is not None:
        return record(name, False, f"抛异常（会穿到框架）：{type(exc).__name__}: {exc}")
    if not isinstance(value, str):
        return record(name, False, f"返回类型是 {type(value).__name__}，不是 str：{value!r}")
    missing = [s for s in must_contain if s not in value]
    if missing:
        return record(name, False, f"缺少 {missing}；实际返回：{value[:200]!r}")
    preview = value.replace("\n", "\\n")[:70]
    return record(name, True, repr(preview))


def has_stack_trace(text: str) -> bool:
    """返回值里是否混进了 Python 堆栈/异常原文。"""
    return any(marker in text for marker in STACK_MARKERS)


def make_plugin(config: dict | None = None):
    """实例化插件（走真实 ``Star.__init__``，config 是 AstrBot 传来的原始 dict）。"""
    inst = MODULE.BskBrowserPlugin(make_context(), config=config)
    INSTANCES.append(inst)
    return inst


def base_config(**overrides) -> dict:
    """一份可用的插件配置；``**overrides`` 用于构造边界场景。"""
    cfg = {
        "enabled": True,
        "bsk_path": BSK_PATH,
        "command_timeout_sec": 60,
        "max_sessions": 3,
        "admin_only": True,
        "session_scope": "umo",
        "idle_release_sec": 240,
        "screenshot_dir": SHOT_DIR,
    }
    cfg.update(overrides)
    return cfg


def session_id_for(inst, key: str) -> str:
    """读插件侧会话表里某个 key 的 bsk session_id（空串 = 还没有会话）。"""
    try:
        for detail in inst.service.sessions.stats().get("details", []):
            if detail.get("key") == key:
                return str(detail.get("session_id") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def started_count(inst) -> int:
    """插件侧累计创建过多少个会话（用于证明"拒绝时没有真的开浏览器"）。"""
    try:
        return int((inst.service.sessions.stats().get("counters") or {}).get("started", -1))
    except Exception:  # noqa: BLE001
        return -1


async def daemon_session_ids() -> set[str]:
    """直接问 daemon 现在有哪些会话 id。

    用 ``bsk session list --json`` 而不是 ``browsers`` 的 ``session_count`` ——
    后者会把**所有**会话算进来（包括用户自己的 DSH），无法区分哪些是本测试创建的。
    """
    result = await RUNNER.run(["session", "list", "--json"], timeout=15)
    data = result.data or []
    if not isinstance(data, list):
        return set()
    return {
        str(item.get("session_id"))
        for item in data
        if isinstance(item, dict) and item.get("session_id")
    }


def collect_live_session_ids() -> set[str]:
    """汇总所有插件实例当前持有的 session_id（清理前调用）。"""
    ids: set[str] = set()
    for inst in INSTANCES:
        with contextlib.suppress(Exception):
            for detail in inst.service.sessions.stats().get("details", []):
                sid = str(detail.get("session_id") or "")
                if sid:
                    ids.add(sid)
    return ids


def png_info(path: str) -> tuple[bool, str]:
    """检查文件存在且是 PNG 魔数。"""
    target = Path(path)
    if not target.is_file():
        return False, f"文件不存在：{path}"
    head = target.read_bytes()[:8]
    size = target.stat().st_size
    return head == PNG_MAGIC, f"文件头={head.hex(' ').upper()}，{size} 字节"


# ---------------------------------------------------------------------------
# 用例组 1：权限门（最重要）
# ---------------------------------------------------------------------------


async def group_permission() -> None:
    banner("用例组 1：权限门")

    before = await daemon_session_ids()

    # --- 1.1 默认 admin_only=True，非管理员被拒绝 ---
    inst = make_plugin(base_config())
    denied, exc = await call(
        inst.bsk_open, FakeEvent(is_admin=False, sender_id="10001", umo=UMO_BROWSER), url=URL_OK
    )
    check_str_result("1.1 非管理员调用 bsk_open 被拒绝", denied, exc, ("仅限管理员",))

    # --- 1.2 拒绝时**没有真的打开浏览器**（daemon 会话清单前后对比）---
    after = await daemon_session_ids()
    record(
        "1.2 拒绝时没有新建 bsk 会话（daemon 前后对比）",
        after == before and started_count(inst) == 0,
        f"before={sorted(before)} after={sorted(after)}，插件 started={started_count(inst)}",
    )

    # --- 1.3 管理员放行 ---
    # 这里故意用一个"必然在权限门之后才失败"的输入（空网址）来证明**已经过了权限门**：
    # 空网址的提示文案与权限拒绝文案完全不同，能明确区分"被拒"与"放行"。
    text, exc = await call(
        inst.bsk_open, FakeEvent(is_admin=True, sender_id="10001", umo=UMO_BROWSER), url=""
    )
    ok = check_str_result("1.3 管理员通过权限门", text, exc, ("请提供要打开的网址",))
    if ok and "仅限管理员" in text:
        record("1.3b 放行判定未与拒绝文案混淆", False, "同时出现了权限拒绝文案")
    elif ok:
        record("1.3b 放行判定未与拒绝文案混淆", True, "返回的是网址校验提示，不是权限拒绝")

    # --- 1.4 admin_only=False：非管理员放行（真实打开一次，证明端到端通过）---
    inst_open = make_plugin(base_config(admin_only=False))
    ev_nonadmin = FakeEvent(is_admin=False, sender_id="10002", umo=UMO_NONADMIN)
    text, exc = await call(inst_open.bsk_open, ev_nonadmin, url=URL_OK)
    check_str_result(
        "1.4 admin_only=False 时非管理员可真实打开网页",
        text,
        exc,
        ("已打开：", "Example Domain"),
    )

    # --- 1.5 白名单优先：名单内放行、名单外拒绝 ---
    inst_wl = make_plugin(base_config(admin_only=True, allowed_users=("1",)))
    # 同一种输入（空网址），只有 sender_id 不同 → 返回文案必须不同，才能证明是白名单在起作用。
    allowed, exc_a = await call(
        inst_wl.bsk_open, FakeEvent(is_admin=False, sender_id="1", umo=UMO_BROWSER), url=""
    )
    blocked, exc_b = await call(
        inst_wl.bsk_open, FakeEvent(is_admin=False, sender_id="2", umo=UMO_BROWSER), url=""
    )
    record(
        "1.5 白名单内用户（sender_id=1）放行",
        exc_a is None and isinstance(allowed, str) and "请提供要打开的网址" in allowed,
        f"{(allowed if isinstance(allowed, str) else exc_a)!r}"[:120],
    )
    check_str_result("1.6 白名单外用户（sender_id=2）被拒绝", blocked, exc_b, ("白名单",))

    # --- 1.7 bsk_screenshot 的权限门（async generator 分支）---
    # 这是独立的一条代码路径：拒绝时它必须 yield 一次然后 return，而不是抛异常。
    items, gen_exc = await consume(
        inst.bsk_screenshot(FakeEvent(is_admin=False, sender_id="10001", umo=UMO_BROWSER))
    )
    ok = (
        gen_exc is None
        and len(items) == 1
        and isinstance(items[0], str)
        and "仅限管理员" in items[0]
    )
    record(
        "1.7 非管理员调用 bsk_screenshot 只 yield 一条拒绝文本",
        ok,
        f"items={items!r} exc={gen_exc!r}",
    )

    # --- 1.8 权限拒绝不产生任何会话（再确认一次）---
    final = await daemon_session_ids()
    record(
        "1.8 整组权限用例未产生额外会话",
        final == before,
        f"before={sorted(before)} after={sorted(final)}",
    )


# ---------------------------------------------------------------------------
# 用例组 2：参数校验（不发网络请求就能测）
# ---------------------------------------------------------------------------


async def group_validation() -> None:
    banner("用例组 2：参数校验（全部必须返回字符串而不是抛异常）")

    inst = make_plugin(base_config())
    admin = FakeEvent(is_admin=True, sender_id="10001", umo=UMO_BROWSER)
    started_before = started_count(inst)

    # 2.1 空网址
    text, exc = await call(inst.bsk_open, admin, url="")
    check_str_result("2.1 bsk_open(url='') 拒绝并提示要提供网址", text, exc, ("请提供要打开的网址",))

    # 2.2 协议不对
    text, exc = await call(inst.bsk_open, admin, url="ftp://x")
    check_str_result(
        "2.2 bsk_open(url='ftp://x') 拒绝并说明必须 http(s)",
        text,
        exc,
        ("http://", "https://"),
    )

    # 2.3 缺协议
    text, exc = await call(inst.bsk_open, admin, url="example.com")
    check_str_result(
        "2.3 bsk_open(url='example.com') 缺协议被拒绝",
        text,
        exc,
        ("http://", "https://"),
    )

    # 2.4 click 缺 target
    text, exc = await call(inst.bsk_act, admin, action="click")
    check_str_result("2.4 bsk_act(click) 缺 target 返回可读错误", text, exc, ("操作失败", "点击"))

    # 2.5 press 缺 key_spec
    text, exc = await call(inst.bsk_act, admin, action="press")
    check_str_result("2.5 bsk_act(press) 缺 key_spec 返回可读错误", text, exc, ("操作失败", "键"))

    # 2.6 非法动作名
    text, exc = await call(inst.bsk_act, admin, action="not_a_real_action")
    ok = check_str_result(
        "2.6 bsk_act(非法动作) 返回不支持并列出可用动作",
        text,
        exc,
        ("不支持的动作", "click", "scroll_to"),
    )
    if ok:
        note(f"可用动作清单：{text[:150]}")

    # 2.7 select 缺 value
    text, exc = await call(inst.bsk_act, admin, action="select", target="@e1")
    check_str_result("2.7 bsk_act(select) 缺 value 返回可读错误", text, exc, ("操作失败", "选项"))

    # 2.8 参数校验失败**不能**顺手开一个浏览器会话
    started_after = started_count(inst)
    record(
        "2.8 参数校验失败未创建任何浏览器会话",
        started_after == started_before == 0,
        f"started: {started_before} → {started_after}",
    )


# ---------------------------------------------------------------------------
# 用例组 3：真实浏览器流程（只读）
# ---------------------------------------------------------------------------


async def group_real_browser() -> None:
    banner("用例组 3：真实浏览器流程（只读，仅访问 example.com）")

    inst = make_plugin(base_config())
    admin = FakeEvent(is_admin=True, sender_id="10001", umo=UMO_BROWSER)

    # 3.1 打开网页
    text, exc = await call(inst.bsk_open, admin, url=URL_OK)
    check_str_result("3.1 bsk_open 打开 example.com", text, exc, ("已打开：", "Example Domain"))
    sid = session_id_for(inst, UMO_BROWSER)
    note(f"本次会话 session_id={sid or '(未建立)'}")

    # 3.2 读取页面
    text, exc = await call(inst.bsk_read, admin)
    ok = check_str_result("3.2 bsk_read 返回页面摘要含标题", text, exc, ("Example Domain",))
    if ok:
        refs = re.findall(r"@e\d+", text)
        record(
            "3.3 bsk_read 返回可用的元素引用（@eN）",
            bool(refs),
            f"引用={refs[:8]}" if refs else f"没找到 @eN；返回：{text[:200]!r}",
        )

    # 3.4 截图（async generator：先图后文）
    items, gen_exc = await consume(inst.bsk_screenshot(admin))
    if gen_exc is not None:
        record("3.4 bsk_screenshot 正常产出一张图片", False, f"生成器抛异常：{gen_exc!r}")
    else:
        record(
            "3.4 bsk_screenshot 至少 yield 两项",
            len(items) >= 2,
            f"共 {len(items)} 项：{[type(i).__name__ for i in items]}",
        )
        image_items = [i for i in items if hasattr(i, "path")]
        text_items = [i for i in items if isinstance(i, str)]
        if not image_items:
            record("3.5 bsk_screenshot 产出图片结果", False, f"没有带 .path 的项：{items!r}")
        else:
            ok_png, detail = png_info(str(image_items[0].path))
            record(
                "3.5 bsk_screenshot 产出的图片真实存在且是 PNG 魔数",
                ok_png,
                f"{image_items[0].path}；{detail}",
            )
        record(
            "3.6 bsk_screenshot 同时 yield 描述文本",
            bool(text_items) and "截图" in text_items[-1],
            f"{text_items[-1][:120]!r}" if text_items else "没有文本项",
        )
        # 顺序保证：图片先 yield（会被框架 set_result 发给用户），文本后 yield（回灌模型）
        if image_items and text_items:
            record(
                "3.7 图片先于文本 yield（符合框架消费顺序）",
                items.index(image_items[0]) < len(items) - 1 - items[::-1].index(text_items[-1]),
                f"顺序={[type(i).__name__ for i in items]}",
            )

    # 3.8 只读动作：scroll_to @e1
    text, exc = await call(inst.bsk_act, admin, action="scroll_to", target="@e1")
    check_str_result("3.8 bsk_act(scroll_to @e1) 执行成功", text, exc, ("已执行 scroll_to",))

    # 3.9 诊断信息
    text, exc = await call(inst.bsk_status, admin)
    ok = check_str_result("3.9 bsk_status 返回 bsk 路径", text, exc, ("bsk 可执行文件：",))
    if ok:
        record(
            "3.10 bsk_status 返回浏览器连接信息",
            "已连接的浏览器" in text and "edge" in text.lower(),
            f"{text.replace(chr(10), ' | ')[:220]}",
        )

    # 3.11 关闭会话
    text, exc = await call(inst.bsk_close, admin)
    check_str_result("3.11 bsk_close 关闭会话", text, exc, ("已关闭浏览器会话",))

    # 3.12 再关一次
    text, exc = await call(inst.bsk_close, admin)
    check_str_result(
        "3.12 重复 bsk_close 返回没有正在使用的会话",
        text,
        exc,
        ("当前没有正在使用的浏览器会话",),
    )


# ---------------------------------------------------------------------------
# 用例组 4：会话隔离
# ---------------------------------------------------------------------------


async def group_isolation() -> None:
    banner("用例组 4：会话隔离（不同 umo 分开，相同 umo 复用）")

    inst = make_plugin(base_config())
    ev_b1 = FakeEvent(is_admin=True, sender_id="20001", umo=UMO_ISO_B)
    ev_b2 = FakeEvent(is_admin=True, sender_id="20002", umo=UMO_ISO_B)
    ev_c = FakeEvent(is_admin=True, sender_id="20003", umo=UMO_ISO_C)

    text_b1, exc_b1 = await call(inst.bsk_open, ev_b1, url=URL_OK)
    sid_b1 = session_id_for(inst, UMO_ISO_B)
    text_b2, exc_b2 = await call(inst.bsk_open, ev_b2, url=URL_OK)
    sid_b2 = session_id_for(inst, UMO_ISO_B)
    text_c, exc_c = await call(inst.bsk_open, ev_c, url=URL_OK)
    sid_c = session_id_for(inst, UMO_ISO_C)

    if exc_b1 or exc_b2 or exc_c:
        record(
            "4.1 会话隔离用例的基础调用成功",
            False,
            f"异常：{exc_b1!r} {exc_b2!r} {exc_c!r}",
        )
        return

    record("4.1 两个 umo 各自打开了网页", True, f"B={sid_b1!r} C={sid_c!r}")

    record(
        "4.2 不同 umo 产生两个不同的 bsk 会话",
        bool(sid_b1) and bool(sid_c) and sid_b1 != sid_c,
        f"umo_B → {sid_b1!r}，umo_C → {sid_c!r}（必须不同）",
    )
    record(
        "4.3 同一 umo 的两次 bsk_open 复用同一个会话",
        bool(sid_b1) and sid_b1 == sid_b2,
        f"第一次 {sid_b1!r}，第二次 {sid_b2!r}（必须相同）",
    )

    stats = inst.service.sessions.stats()
    counters = stats.get("counters") or {}
    record(
        "4.4 插件侧只创建了 2 个会话（复用生效）",
        counters.get("started") == 2 and stats.get("sessions") == 2,
        f"started={counters.get('started')} sessions={stats.get('sessions')}",
    )

    # daemon 侧交叉验证：这两个 id 必须都真实存在
    live = await daemon_session_ids()
    record(
        "4.5 daemon 侧确实存在这两个会话",
        {sid_b1, sid_c} <= live,
        f"daemon={sorted(live)}，本组={sorted({sid_b1, sid_c})}",
    )

    # 收尾：只关本组自己的两个 key（精确 id，不用 --all）
    await call(inst.bsk_close, ev_b1)
    text, exc = await call(inst.bsk_close, ev_c)
    record(
        "4.6 本组会话已按 key 精确关闭",
        exc is None and isinstance(text, str) and "已关闭浏览器会话" in text,
        f"{text!r}",
    )


# ---------------------------------------------------------------------------
# 用例组 5：异常包装（必然失败的场景）
# ---------------------------------------------------------------------------


async def group_error_wrapping() -> None:
    banner("用例组 5：异常包装（必然失败的域名，检查没有堆栈穿到用户面前）")

    inst = make_plugin(base_config(command_timeout_sec=5))
    admin = FakeEvent(is_admin=True, sender_id="10001", umo=UMO_DEAD)

    started = time.monotonic()
    text, exc = await call(inst.bsk_open, admin, url=URL_DEAD)
    elapsed = time.monotonic() - started

    if exc is not None:
        record(
            "5.1 不存在的域名：不抛异常，返回可读中文",
            False,
            f"抛异常：{type(exc).__name__}: {exc}",
        )
        return

    if not isinstance(text, str):
        record("5.1 不存在的域名：返回 str", False, f"类型 {type(text).__name__}：{text!r}")
        return

    record(
        "5.1 不存在的域名：返回的是字符串而不是抛异常",
        True,
        f"耗时 {elapsed:.1f}s",
    )
    record(
        "5.2 返回值是可读中文错误（无 Python 堆栈）",
        "打开网页失败" in text and not has_stack_trace(text),
        f"{text.replace(chr(10), ' | ')[:200]}",
    )
    record(
        "5.3 错误文案里有可操作的下一步提示",
        any(
            k in text
            for k in ("bsk doctor", "bsk --version", "bsk browsers", "重试", "检查", "浏览器")
        ),
        f"{text.replace(chr(10), ' | ')[:250]}",
    )
    record(
        "5.4 失败场景耗时未超过命令超时上限",
        elapsed < 120,
        f"{elapsed:.1f}s（command_timeout_sec=5，navigate 内部超时 45s）",
    )


# ---------------------------------------------------------------------------
# 用例组 6：图片发送失败时的降级
# ---------------------------------------------------------------------------


async def group_image_failure_fallback() -> None:
    banner("用例组 6：图片发送失败时仍要 yield 文本（且含截图路径）")

    class BrokenImageEvent(FakeEvent):
        """``image_result`` 必抛异常的替身，模拟"发图失败"。"""

        def image_result(self, path):
            raise RuntimeError("模拟发送图片失败")

    inst = make_plugin(base_config())
    ev = BrokenImageEvent(is_admin=True, sender_id="10001", umo=UMO_BROKEN_IMG)

    text, exc = await call(inst.bsk_open, ev, url=URL_OK)
    if exc is not None or not isinstance(text, str) or "Example Domain" not in text:
        record(
            "6.1 降级用例的前置打开成功",
            False,
            f"exc={exc!r} text={str(text)[:150]!r}",
        )
        return
    record("6.1 降级用例的前置打开成功", True, f"session={session_id_for(inst, UMO_BROKEN_IMG)}")

    items, gen_exc = await consume(inst.bsk_screenshot(ev))
    if gen_exc is not None:
        record("6.2 发图失败时生成器不抛异常", False, f"抛异常：{gen_exc!r}")
        return
    record("6.2 发图失败时生成器不抛异常", True, f"共 yield {len(items)} 项")

    text_items = [i for i in items if isinstance(i, str)]
    if not text_items:
        record("6.3 发图失败时仍然 yield 了文本", False, f"yield 项：{items!r}")
        return
    fallback_text = text_items[-1]
    record(
        "6.3 发图失败时仍然 yield 了文本",
        True,
        f"{fallback_text[:150]!r}",
    )

    match = re.search(r"截图文件在：(.+?)）", fallback_text)
    if not match:
        record(
            "6.4 降级文本里包含截图文件路径",
            False,
            f"没匹配到路径；文本：{fallback_text[:250]!r}",
        )
        return
    shot_path = match.group(1)
    ok_png, detail = png_info(shot_path)
    record(
        "6.4 降级文本里的截图路径真实存在且是 PNG",
        ok_png,
        f"{shot_path}；{detail}",
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def run() -> int:
    global BSK_PATH, SHOT_DIR, RUNNER

    BSK_PATH = _resolve_bsk_path()
    SHOT_DIR = str(Path(tempfile.gettempdir()) / "astrbot_bsk_e2e_shots")
    print(f"bsk 路径：{BSK_PATH}")
    print(f"截图目录：{SHOT_DIR}")

    # --- 环境探测 ---
    banner("用例组 0：环境")
    try:
        version = await RUNNER.run(["--version"], timeout=15, expect_json=False)
        record("0.1 bsk 可执行文件可用", version.ok, (version.stdout or version.stderr)[:80])
    except Exception as exc:  # noqa: BLE001
        record("0.1 bsk 可执行文件可用", False, repr(exc))
        return 1

    try:
        browsers = await RUNNER.run(["browsers", "--json"], timeout=15)
        items = browsers.data if isinstance(browsers.data, list) else []
        record(
            "0.2 有已连接的浏览器",
            bool(items),
            f"{len(items)} 个：" + ", ".join(str(b.get('instance_id')) for b in items if isinstance(b, dict)),
        )
        if not items:
            print("\n没有可用浏览器，后续真实用例无法执行。")
            return 1
    except Exception as exc:  # noqa: BLE001
        record("0.2 有已连接的浏览器", False, repr(exc))
        return 1

    initial_ids = await daemon_session_ids()
    note(f"开始前 daemon 里已有会话（不属于本测试）：{sorted(initial_ids)}")

    # --- 用例组 ---
    for group in (
        group_permission,
        group_validation,
        group_real_browser,
        group_isolation,
        group_error_wrapping,
        group_image_failure_fallback,
    ):
        try:
            await group()
        except Exception:  # noqa: BLE001 - 单组崩溃不应终止整轮
            record(f"{group.__name__} 执行异常", False, traceback.format_exc()[-400:])

    return 0


async def cleanup() -> None:
    """关闭本测试创建的所有会话，并精确验证没有残留。

    ★ 断言只比对"**本测试创建的** session id 是否还在 daemon 里"，
      不断言 daemon 会话数为 0 —— 那会把用户自己的 DSH 会话算进来而误报。
    """
    banner("清理：关闭本测试创建的所有会话")

    CREATED_SESSION_IDS.update(collect_live_session_ids())
    print(f"本测试创建过的 session id：{sorted(CREATED_SESSION_IDS) or '（无）'}")

    for inst in INSTANCES:
        with contextlib.suppress(Exception):
            await inst.terminate()

    try:
        remaining = await daemon_session_ids()
        leaked = remaining & CREATED_SESSION_IDS
        record(
            "7.1 清理后本测试创建的会话无残留",
            not leaked,
            (
                f"泄漏 {sorted(leaked)}"
                if leaked
                else f"已全部关闭（daemon 里还有 {len(remaining - CREATED_SESSION_IDS)} 个"
                "不属于本测试的会话）"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        record("7.1 清理后本测试创建的会话无残留", False, repr(exc))


def main() -> int:
    global MODULE, FakeEvent, make_context, BskRunner, RUNNER

    # 一致性检查要在 import 之前做：不一致就没必要再往下跑了。
    if not consistency_check():
        return 1

    _ensure_paths()

    try:
        import astrbot

        note(f"astrbot 版本：{getattr(astrbot, '__version__', 'unknown')}")
    except Exception:
        record("import astrbot", False, traceback.format_exc()[-400:])
        return 1

    try:
        MODULE = __import__(MODULE_NAME, fromlist=["main"])
    except Exception:
        record("import 插件模块", False, traceback.format_exc()[-600:])
        return 1
    record("import 插件模块", True, MODULE_NAME)

    from astrbot_test_doubles import FakeEvent as _FakeEvent  # noqa: PLC0415
    from astrbot_test_doubles import make_context as _make_context  # noqa: PLC0415

    FakeEvent = _FakeEvent
    make_context = _make_context

    BskRunner = _installed_submodule("runner").BskRunner
    RUNNER = BskRunner(_resolve_bsk_path(), default_timeout=15)

    async def flow() -> None:
        try:
            await run()
        finally:
            # 无论中途怎么失败，finally 里都要尽力清理。
            await cleanup()

    asyncio.run(flow())

    # --- 汇总 ---
    print()
    print("=" * 72)
    failed = [r for r in RESULTS if not r[1]]
    print(f"用例数：{len(RESULTS)}，失败数：{len(failed)}")
    for name, _, detail in failed:
        print(f"  [FAIL] {name}: {detail}")
    print("结果：" + ("全部通过" if not failed else "有失败"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
