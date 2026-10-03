"""发布前自检 —— 把"能不能上架"变成一条可重复执行的命令。

发布到 AstrBot 插件市场是网页表单（https://cloud.astrbot.app/publish），
提交前没人会替你检查元数据是否合规。这个脚本把**所有能在本地验证的规则**
一次性跑完，避免提交后被拒或装到用户机器上才发现问题。

检查依据全部来自 AstrBot 4.28.1 源码（不是文档转述）：
- ``star/updater.py:26``  必填字段 ``("name", "desc", "version", "author")``
- ``star/updater.py:357`` 必填字段必须是非空**字符串**
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

    # requirements.txt 其实是**可选**的：AstrBot 在 star_manager.py:359 里
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
    # 6. 打包体积（插件市场对 zip 有上限，源码本身应远小于它）
    # ---------------------------------------------------------------
    print("\n--- 打包体积 ---")
    total = 0
    file_count = 0
    skip_dirs = {".git", "__pycache__", ".pytest_cache"}
    for path in PROJECT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        total += path.stat().st_size
        file_count += 1
    mb = total / (1024 * 1024)
    check(
        "源码体积合理（< 1MB，市场 zip 上限 16MB）",
        mb < 1.0,
        f"{file_count} 个文件，共 {mb:.2f} MB",
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
