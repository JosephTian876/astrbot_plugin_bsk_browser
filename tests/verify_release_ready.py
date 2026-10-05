"""发布前自检 —— 把"能不能上架"变成一条可重复执行的命令。

发布到 AstrBot 插件市场是网页表单（https://cloud.astrbot.app/publish），
提交前没人会替你检查元数据是否合规。这个脚本把所有能在本地验证的规则
一次性跑完，避免提交后被拒或装到用户机器上才发现问题。

检查依据全部来自 AstrBot 4.28.1 源码（不是文档转述）：
- ``star/updater.py:26``  必填字段 ``("name", "desc", "version", "author")``
- ``star/updater.py:357`` 必填字段必须是非空字符串
- ``star/star_manager.py:628`` 插件目录名必须是合法 Python 标识符
- ``star/star_manager.py:671`` ``astrbot_version`` 必须是合法版本约束
- ``config/default.py`` ``DEFAULT_VALUE_MAP`` 配置项 type 白名单

用法：
    python tests/verify_release_ready.py
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

ASTRBOT_APP = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
if os.path.isdir(ASTRBOT_APP):
    sys.path.insert(0, ASTRBOT_APP)

# 钉住 AstrBot 的 root，避免它在项目目录里生成 data/。
# 本脚本要 import AstrBot 的校验器；若不同时设定 root，AstrBot 会把当前
# 工作目录当 root（astrbot_path.py:35），就地生成含主配置的 data/ 目录。
os.environ.setdefault(
    "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def warn(name: str, detail: str) -> None:
    print(f"[warn] {name} —— {detail}")


# AstrBot 配置项的合法 type（core/config/default.py 的 DEFAULT_VALUE_MAP）
ALLOWED_CONFIG_TYPES = {
    "int",
    "float",
    "bool",
    "string",
    "text",
    "list",
    "file",
    "object",
    "template_list",
    "dict",
}


def load_metadata() -> dict:
    """解析 metadata.yaml。用 AstrBot 自带 yaml，保证与运行时一致。"""
    import yaml

    path = PROJECT / "metadata.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def main() -> int:
    print("=" * 72)
    print("发布前自检")
    print("=" * 72)

    # ---------------------------------------------------------------
    # 1. 必需文件
    # ---------------------------------------------------------------
    print("\n--- 文件完整性 ---")
    required_files = {
        "main.py": "插件入口（AstrBot 只认 main.py 或与目录同名的 .py）",
        "metadata.yaml": "插件清单",
        "_conf_schema.json": "配置 schema",
        "README.md": "说明文档",
        "LICENSE": "开源许可",
    }
    for fname, why in required_files.items():
        exists = (PROJECT / fname).is_file()
        check(f"存在 {fname}", exists, "" if exists else f"缺少：{why}")

    # requirements.txt 其实是可选的：AstrBot 在 star_manager.py:359 里
    # 用的是 `if not os.path.exists(...): return`，缺文件直接跳过、不报错。
    # 但惯例上都会带一个（哪怕是空的），这样别人一看就知道本插件没有额外依赖。
    req = PROJECT / "requirements.txt"
    if req.is_file():
        # 有内容但没有实际依赖（全是注释/空行）也要能看出来
        lines = [
            ln.strip()
            for ln in req.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        check(
            "requirements.txt 已提供",
            True,
            f"{len(lines)} 条实际依赖" + ("（纯标准库，无第三方依赖）" if not lines else f"：{lines}"),
        )
    else:
        warn(
            "requirements.txt 缺失",
            "AstrBot 允许缺失（会跳过），但建议加一个空文件表明本插件无第三方依赖",
        )

    # ---------------------------------------------------------------
    # 2. metadata.yaml（AstrBot 会强校验这四个字段）
    # ---------------------------------------------------------------
    print("\n--- metadata.yaml ---")
    try:
        meta = load_metadata()
        check("可解析为字典", isinstance(meta, dict), f"类型={type(meta).__name__}")
    except Exception as exc:  # noqa: BLE001
        check("可解析为字典", False, repr(exc))
        meta = {}

    required_fields = ("name", "desc", "version", "author")
    for field in required_fields:
        present = field in meta
        is_nonempty_str = present and isinstance(meta[field], str) and bool(meta[field].strip())
        check(
            f"必填字段 {field}（非空字符串）",
            is_nonempty_str,
            f"值={meta.get(field)!r}" if present else "缺失",
        )

    # 目录名必须是合法 Python 标识符（AstrBot 用它做 import 路径）
    dir_name = PROJECT.name
    check(
        f"目录名是合法 Python 标识符（{dir_name}）",
        dir_name.isidentifier() and not __import__("keyword").iskeyword(dir_name),
        "AstrBot 用 __import__ 加载插件，非法标识符会导致加载失败",
    )
    check(
        "name 与目录名一致",
        meta.get("name") == dir_name,
        f"name={meta.get('name')!r} vs 目录={dir_name!r}",
    )

    # 版本号
    version = str(meta.get("version", ""))
    check(
        "version 形如 x.y.z",
        bool(re.fullmatch(r"\d+\.\d+\.\d+", version)),
        f"version={version!r}",
    )

    # astrbot_version 必须是合法约束（用 AstrBot 自己的校验函数）
    spec = meta.get("astrbot_version")
    if spec:
        try:
            from astrbot.core.star.star_manager import PluginManager

            ok = PluginManager._validate_astrbot_version_specifier(str(spec))
            check(
                f"astrbot_version 合法（{spec}）",
                bool(ok),
                "" if ok else "AstrBot 校验函数判定该约束不合法",
            )
        except Exception as exc:  # noqa: BLE001
            check(f"astrbot_version 合法（{spec}）", False, repr(exc))
    else:
        warn("astrbot_version", "未声明，AstrBot 将不做版本检查（建议声明以免装到不兼容版本上）")

    # 用 AstrBot 自己的校验函数复核一遍
    try:
        from astrbot.core.star.updater import _PluginUpdater

        _PluginUpdater.validate_plugin_metadata(meta, "metadata.yaml")
        check("通过 AstrBot 原生 metadata 校验", True, "")
    except Exception as exc:  # noqa: BLE001
        check("通过 AstrBot 原生 metadata 校验", False, repr(exc))

    # 占位符提醒（发布前必须替换）
    for field in ("author", "repo"):
        val = str(meta.get(field, ""))
        if "yourname" in val or "yourname" in str(meta.get("repo", "")):
            warn(f"{field} 仍是占位符", f"{field}={val!r}，发布前请替换成你自己的信息")

    # ---------------------------------------------------------------
    # 3. _conf_schema.json（非法 type 会让插件加载失败）
    # ---------------------------------------------------------------
    print("\n--- _conf_schema.json ---")
    try:
        schema = json.loads((PROJECT / "_conf_schema.json").read_text(encoding="utf-8"))
        check("是合法 JSON", True, f"{len(schema)} 项配置")

        bad_types = [
            (k, v.get("type"))
            for k, v in schema.items()
            if v.get("type") not in ALLOWED_CONFIG_TYPES
        ]
        check(
            "所有 type 在白名单内",
            not bad_types,
            f"非法：{bad_types}" if bad_types else "",
        )

        missing_default = [k for k, v in schema.items() if "default" not in v]
        check("每项都有 default", not missing_default, f"缺少：{missing_default}")

        # 与 config.py 的默认值一致性（有专门脚本，这里做轻量复核）
        from bsk.config import parse_settings

        settings = parse_settings({})
        mismatches = []
        for key, spec_obj in schema.items():
            if not hasattr(settings, key):
                mismatches.append(f"{key}(Settings 中缺失)")
                continue
            actual = getattr(settings, key)
            if isinstance(actual, tuple):
                actual = list(actual)
            expected = spec_obj.get("default")
            if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                same = float(expected) == float(actual)
            else:
                same = expected == actual
            if not same:
                mismatches.append(f"{key}(schema={expected!r} 实际={actual!r})")
        check(
            "schema 默认值与 config.py 一致",
            not mismatches,
            f"不一致：{mismatches}" if mismatches else "",
        )
    except Exception as exc:  # noqa: BLE001
        check("_conf_schema.json 检查", False, repr(exc))

    # ---------------------------------------------------------------
    # 4. 合规红线（不捆绑 bsk、不复制上游代码、非官方声明）
    # ---------------------------------------------------------------
    print("\n--- 合规红线 ---")

    # 4a. 绝不能捆绑 bsk 二进制
    binary_hits = [
        p.relative_to(PROJECT).as_posix()
        for p in PROJECT.rglob("*")
        if p.is_file()
        and p.suffix.lower() in {".exe", ".dll", ".so", ".dylib", ".node"}
        and ".git" not in p.parts
    ]
    check(
        "不含任何二进制可执行文件（不得捆绑 bsk）",
        not binary_hits,
        f"发现：{binary_hits}" if binary_hits else "",
    )

    # 4b. 插件名不得使用 BrowserSkill 商标作为产品名
    plugin_name = str(meta.get("name", ""))
    display = str(meta.get("display_name", ""))
    check(
        "插件名未使用 BrowserSkill 商标",
        "browserskill" not in plugin_name.lower().replace("_", ""),
        f"name={plugin_name!r}",
    )
    if "browserskill" in display.lower().replace(" ", ""):
        warn("display_name 含 BrowserSkill", f"{display!r} —— 建议改用描述性名称以避免商标混淆")

    # 4c. README 必须含非官方声明与致谢
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    check(
        "README 含「非官方」声明",
        "非官方" in readme,
        "MIT 不授予商标权，必须声明与腾讯无隶属关系",
    )
    check(
        "README 致谢 Tencent/BrowserSkill",
        "Tencent/BrowserSkill" in readme,
        "",
    )
    check(
        "README 说明不包含 bsk",
        "不包含" in readme and "bsk" in readme,
        "需明确声明不再分发 bsk",
    )

    # 4d. LICENSE 存在且是双许可
    license_text = (PROJECT / "LICENSE").read_text(encoding="utf-8")
    check(
        "LICENSE 是双许可声明",
        "MIT" in license_text and "AGPL" in license_text,
        "",
    )

    # ---------------------------------------------------------------
    # 5. 架构约束（防止将来重构破坏，见 ARCHITECTURE.md §2）
    # ---------------------------------------------------------------
    print("\n--- 架构约束 ---")

    main_src = (PROJECT / "main.py").read_text(encoding="utf-8")
    main_tree = ast.parse(main_src)

    # C3：绝不能定义 __del__（会让 terminate 永不执行）
    has_del = any(
        isinstance(node, ast.FunctionDef) and node.name == "__del__"
        for node in ast.walk(main_tree)
    )
    check("main.py 未定义 __del__（C3）", not has_del, "定义 __del__ 会让 terminate() 永不执行")

    # C2：@filter.llm_tool 必须定义在 main.py
    tool_names = re.findall(r'@filter\.llm_tool\(\s*"([^"]+)"\s*\)', main_src)
    check(
        "llm_tool 全部定义在 main.py（C2）",
        len(tool_names) >= 6,
        f"发现 {len(tool_names)} 个：{tool_names}",
    )

    # bsk/ 包绝不能 import astrbot（否则无法脱离框架单测）
    bsk_dir = PROJECT / "bsk"
    offenders = []
    for py in bsk_dir.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name.split(".")[0] == "astrbot" for a in node.names):
                    offenders.append(py.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] == "astrbot":
                    offenders.append(py.name)
    check(
        "bsk/ 包不 import astrbot（分层约束）",
        not offenders,
        f"违反：{sorted(set(offenders))}" if offenders else "",
    )

    # 只允许 main.py 依赖框架
    check(
        "main.py 是唯一 import astrbot 的文件",
        "astrbot" in main_src,
        "",
    )

    # ---------------------------------------------------------------
    # 6. 打包体积
    # ---------------------------------------------------------------
    # 插件市场对上传 zip 有 16MB 上限。本检查的真正价值是抓住"误提交大文件"
    # （例如把测试截图、视频、bsk 二进制提交进来），而不是卡一个好看的源码体积。
    #
    # 早先这里用 "< 1MB" 的硬阈值 —— 那是我当初拍的数，在合法的插件图标
    # （logo.png，AstrBot 要求必须叫这个名字，约 92KB）存在后就会误报。
    # 与其把阈值抬高了事，不如分成两条更有指向性的检查：
    #   ① 相对真实上限留足余量（这才是市场会拒的原因）
    #   ② 单个文件不得过大（真正导致体积失控的是某个大文件，不是文件数量）
    print("\n--- 打包体积 ---")
    MARKET_ZIP_LIMIT_MB = 16.0
    total = 0
    file_count = 0
    largest: tuple[str, int] = ("", 0)
    skip_dirs = {".git", "__pycache__", ".pytest_cache"}
    for path in PROJECT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        size = path.stat().st_size
        total += size
        file_count += 1
        if size > largest[1]:
            largest = (path.relative_to(PROJECT).as_posix(), size)
    mb = total / (1024 * 1024)

    # ① 相对真实上限：留 8 倍余量（2MB），足够宽松又能在真出问题时报警
    check(
        f"总体积远低于市场 zip 上限（{MARKET_ZIP_LIMIT_MB:.0f}MB）",
        mb < 2.0,
        f"{file_count} 个文件，共 {mb:.2f} MB（上限 {MARKET_ZIP_LIMIT_MB:.0f}MB）",
    )

    # ② 单文件检查：任何单个文件超过 1MB 都值得警惕（图标/许可文本都远小于它）
    largest_mb = largest[1] / (1024 * 1024)
    check(
        "无异常大的单文件（>1MB 通常是误提交了产物）",
        largest[1] < 1024 * 1024,
        f"最大文件：{largest[0]}（{largest_mb:.2f} MB）"
        if largest[0]
        else "",
    )

    # ---------------------------------------------------------------
    # 7. 工作区卫生：不得残留 AstrBot 运行期目录
    # ---------------------------------------------------------------
    # AstrBot 解析数据路径时用「当前工作目录」当 root（astrbot_path.py:35），
    # 任何在项目目录下 import astrbot 的脚本都会就地生成 data/，
    # 里面是 AstrBot 主配置（可能含 API 密钥、管理员 QQ 号）。
    # 这个目录绝不能进入版本库，也绝不该留在源码目录里。
    print("\n--- 工作区卫生 ---")
    runtime_dirs = ("data", "shots", "runtime")
    present = [d for d in runtime_dirs if (PROJECT / d).exists()]
    check(
        "源码目录无 AstrBot 运行期产物",
        not present,
        (
            f"发现 {present} —— 这是 AstrBot 在本目录 import 时生成的，"
            "必须删除且不要提交；若在版本库里请立即 git rm --cached"
        )
        if present
        else "",
    )

    # 这些目录必须在 .gitignore 里
    try:
        gitignore = (PROJECT / ".gitignore").read_text(encoding="utf-8")
        missing_ignores = [
            f"{d}/" for d in runtime_dirs if f"{d}/" not in gitignore
        ]
        check(
            ".gitignore 已覆盖运行期目录",
            not missing_ignores,
            f"缺少：{missing_ignores}" if missing_ignores else "",
        )
    except Exception as exc:  # noqa: BLE001
        check(".gitignore 检查", False, repr(exc))

    # 静态扫描：所有会 import astrbot 的测试脚本都必须先钉住 ASTRBOT_ROOT。
    #
    # 这是一个反复复发的问题：AstrBot 用「当前工作目录」当 root 解析数据
    # 路径（astrbot_path.py:35），任何在项目目录下 import astrbot 的脚本都会
    # 就地生成 data/cmd_config.json（AstrBot 主配置，含 API 密钥）。
    # 已经栽过 3 次，所以改成自动检查而不是靠人记得。
    #
    # 注意：只认真实的 import 语句，不能用关键字搜索 ——
    # 注释里提到 "AstrBotConfig" 会被误判（初版即如此，误报了 test_config.py）。
    def _imports_astrbot(tree: ast.AST) -> bool:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name.split(".")[0] == "astrbot" for a in node.names):
                    return True
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] == "astrbot":
                    return True
            elif isinstance(node, ast.Call):
                # 覆盖 importlib.import_module("astrbot...") 这类动态导入
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if name == "import_module":
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            if arg.value.split(".")[0] == "astrbot":
                                return True
        return False

    offenders: list[str] = []
    for py_file in sorted((PROJECT / "tests").glob("*.py")):
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content)
        except Exception:  # noqa: BLE001
            continue
        if not _imports_astrbot(tree):
            continue
        guards_root = "ASTRBOT_ROOT" in content and "setdefault" in content
        # 只复制 git 已跟踪文件并放进临时目录的脚本不受影响
        uses_temp_root = "fake_root" in content or "tmp_root" in content
        if not (guards_root or uses_temp_root):
            offenders.append(py_file.name)
    check(
        "所有 import astrbot 的测试脚本都钉住了 ASTRBOT_ROOT",
        not offenders,
        (
            f"未设防的脚本：{offenders} —— 它们会在项目目录里生成 "
            "data/cmd_config.json（含 AstrBot 主配置与 API 密钥）"
        )
        if offenders
        else "",
    )

    # ---------------------------------------------------------------
    # 8. 面向用户的文案：不得混入 Markdown 标记
    # ---------------------------------------------------------------
    # 背景：工具返回值与日志会被原样发到 QQ / 微信等聊天平台，那里不渲染
    # Markdown，所以文案里写加粗标记只会让用户看到一对星号。装饰星号同理 ——
    # 混在提示里同样突兀，用户最初反馈的"AI 痕迹"里它最刺眼。
    #
    # 判据：标记出现在"值会被用到"的字符串里才算问题。用 AST 把所有
    # "裸字符串表达式语句"的位置标记出来排除掉 —— 它们的值会被直接丢弃，
    # 用户不可能看到。这一条同时覆盖了三类：模块/类/函数 docstring、
    # `X = ...` 后面紧跟的说明字符串、以及类属性下方的 docstring。
    #
    # 取舍：这比"只查 `return`/`yield` 的常量"宽（能抓到 `problems.append(...)`
    # 这类"返回给调用方、再由调用方打日志"的形态，实测真出过漏网），又比
    # "逐个判断 AST 位置"简单可靠 —— 一个被丢弃的值不可能是用户可见文案。
    #
    # 刻意不检查 `_conf_schema.json`：AstrBot 配置页是用 markdown-it 渲染
    # `hint`/`description` 的（WebUI 的 ConfigPage 经 DashboardTwoFactorDialog
    # chunk 调 `renderInline` 后写入 innerHTML，已实测确认），那里的加粗标记会
    # 正常显示成粗体，不是星号。所以"schema 里有标记却没报警"是有意为之，
    # 不是漏检，请不要去"修"它。
    print("\n--- 面向用户的文案 ---")

    # 用 chr() 而不是字面量：本文件自身也在"不得含装饰星号"的语境里，
    # 写字面量会让源码里出现那个符号，读代码的人反而要多想一层。
    STAR = chr(0x2605)
    BOLD = chr(0x2A) * 2

    def _discarded_string_spans(tree: ast.AST) -> list[tuple[int, int, int, int]]:
        """收集"值被丢弃的字符串常量"的位置区间（用于排除）。"""
        spans: list[tuple[int, int, int, int]] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                val = node.value
                spans.append(
                    (
                        val.lineno,
                        val.col_offset,
                        val.end_lineno or val.lineno,
                        val.end_col_offset or 0,
                    )
                )
        return spans

    # Python 3.12 起 f-string 不再产出 STRING token，字面段是 FSTRING_MIDDLE，
    # 漏掉它就会放过 f-string 里的加粗标记。
    import io
    import tokenize

    string_token_types = {tokenize.STRING}
    if hasattr(tokenize, "FSTRING_MIDDLE"):  # pragma: no cover - 版本相关
        string_token_types.add(tokenize.FSTRING_MIDDLE)

    MARKS = (BOLD, STAR)
    product_files = [PROJECT / "main.py", *sorted((PROJECT / "bsk").glob("*.py"))]
    offenders = []
    for py_file in product_files:
        src = py_file.read_text(encoding="utf-8")
        rel = py_file.relative_to(PROJECT).as_posix()
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            offenders.append(f"{rel}(无法解析：{exc})")
            continue
        spans = _discarded_string_spans(tree)
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type not in string_token_types:
                continue
            hit = [m for m in MARKS if m in tok.string]
            if not hit:
                continue
            end = getattr(tok, "end", None) or tok.start
            if any(
                (r1, c1) <= tok.start and end <= (r2, c2)
                for r1, c1, r2, c2 in spans
            ):
                continue  # docstring / 说明字符串：值被丢弃，用户看不到
            shown = "、".join("加粗标记" if m == BOLD else "装饰星号" for m in hit)
            offenders.append(
                f"{rel}:{tok.start[0]} 含 {shown}"
                f"（{' '.join(tok.string.split())[:48]}）"
            )
    check(
        "产品代码无面向用户的 Markdown 标记",
        not offenders,
        ("；".join(offenders[:5]) + ("…" if len(offenders) > 5 else ""))
        if offenders
        else "聊天与日志都不渲染 Markdown，星号会被用户原样看到",
    )

    # 装饰星号是纯粹的修饰符号，产品代码里一个都不该有（注释里也不行）：
    # 用户最初反馈的"AI 痕迹"里它最刺眼，这里防止它再长回来。
    star_files = [
        py_file.relative_to(PROJECT).as_posix()
        for py_file in product_files
        if STAR in py_file.read_text(encoding="utf-8")
    ]
    check(
        "产品代码不含装饰星号",
        not star_files,
        f"发现：{star_files}" if star_files else "",
    )

    # ---------------------------------------------------------------
    # 汇总
    # ---------------------------------------------------------------
    print()
    print("=" * 72)
    failed = [r for r in RESULTS if not r[1]]
    print(f"检查项：{len(RESULTS)}，失败：{len(failed)}")
    for name, _, detail in failed:
        print(f"  [FAIL] {name}: {detail}")
    print("结果：" + ("全部通过" if not failed else "有失败"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
