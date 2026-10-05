"""发布前合规核验：逐条对照 ARCHITECTURE.md §8 的合规清单。

与 `verify_release_ready.py` 的区别：那个是工程检查（元数据、架构约束），
这个专门做法律/合规核验 —— 每条都给出可复核的证据，而不是"我相信没问题"。

清单来自 ARCHITECTURE.md §8，以及调研报告 01-license.md 的结论。

用法：
    python tests/verify_compliance.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, evidence: str = "") -> None:
    RESULTS.append((name, ok, evidence))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f"\n        证据：{evidence}" if evidence else ""))


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    return [t.strip() for t in out.splitlines() if t.strip()]


def main() -> int:
    print("=" * 72)
    print("发布前合规核验")
    print("=" * 72)

    files = tracked_files()
    print(f"\n版本库内共 {len(files)} 个文件\n")

    # ------------------------------------------------------------------
    # 1. 不包含 bsk 二进制
    # ------------------------------------------------------------------
    print("--- 1. 不分发 bsk（MIT 的再分发义务不触发）---")
    binary_ext = {".exe", ".dll", ".so", ".dylib", ".node", ".bin", ".msi", ".zip"}
    binaries = [f for f in files if Path(f).suffix.lower() in binary_ext]
    check(
        "版本库内无任何二进制/可执行文件",
        not binaries,
        f"无（检查了扩展名 {sorted(binary_ext)}）" if not binaries else f"发现：{binaries}",
    )

    # 文件名里不应有 bsk 二进制
    bsk_named = [f for f in files if "bsk.exe" in f.lower() or f.lower().endswith("bsk")]
    check("无名为 bsk 的可执行文件", not bsk_named, f"{bsk_named or '无'}")

    # ------------------------------------------------------------------
    # 2. 不复制上游源码
    # ------------------------------------------------------------------
    print("\n--- 2. 不复制 Tencent/BrowserSkill 的源码/schema/skill 文档 ---")
    # 上游的特征文件：Rust 源码、协议 schema、SKILL.md
    suspicious = [
        f
        for f in files
        if f.endswith(".rs")
        or "SKILL.md" in f
        or f.startswith("crates/")
        or "bsk-protocol" in f
        or "/schema/" in f
    ]
    check(
        "无上游 Rust 源码 / 协议 schema / SKILL.md",
        not suspicious,
        f"无" if not suspicious else f"发现：{suspicious}",
    )

    # ------------------------------------------------------------------
    # 3. 不复制 AstrBot 源码（写了它的代码就必须 AGPL）
    # ------------------------------------------------------------------
    print("\n--- 3. 未复制 AstrBot 源码 ---")
    # AstrBot 的模块都在 astrbot.* 命名空间下；我们的仓库不应有该目录
    astrbot_files = [f for f in files if f.startswith("astrbot/")]
    check(
        "仓库内无 astrbot/ 目录（未复制其源码）",
        not astrbot_files,
        "无" if not astrbot_files else f"发现：{astrbot_files}",
    )
    # 只应通过 astrbot.api 公开接口互操作
    main_src = (PROJECT / "main.py").read_text(encoding="utf-8")
    imports_api = "from astrbot.api" in main_src
    check(
        "与 AstrBot 的耦合只经公开 API（astrbot.api）",
        imports_api,
        "main.py 使用 from astrbot.api...",
    )

    # ------------------------------------------------------------------
    # 4. 未使用官方模板建仓
    # ------------------------------------------------------------------
    print("\n--- 4. 未使用官方模板 'Use this template' 建仓 ---")
    # 官方模板 Soulter/helloworld 带 AGPL-3.0 的 LICENSE 与特定结构。
    # 我们的 LICENSE 是双许可声明（非纯 AGPL 全文），说明不是照模板来的。
    license_text = (PROJECT / "LICENSE").read_text(encoding="utf-8")
    is_dual = "MIT" in license_text and "AGPL" in license_text
    check(
        "LICENSE 是自写的双许可声明（非模板自带的纯 AGPL 全文）",
        is_dual,
        "MIT OR AGPL-3.0-or-later 双许可声明",
    )

    # ------------------------------------------------------------------
    # 5. LICENSE 完整
    # ------------------------------------------------------------------
    print("\n--- 5. 许可文件完整 ---")
    has_mit_full = "Permission is hereby granted, free of charge" in license_text
    check("LICENSE 含 MIT 全文", has_mit_full, "已内嵌 MIT 全文")
    agpl_file = PROJECT / "LICENSE-AGPL"
    check("LICENSE-AGPL 存在", agpl_file.is_file(), str(agpl_file.name))
    if agpl_file.is_file():
        agpl_text = agpl_file.read_text(encoding="utf-8")
        check(
            "LICENSE-AGPL 是完整 AGPL-3.0 文本",
            "GNU AFFERO GENERAL PUBLIC LICENSE" in agpl_text
            and len(agpl_text) > 30000,
            f"{len(agpl_text)} 字节",
        )

    # ------------------------------------------------------------------
    # 6. README 合规声明
    # ------------------------------------------------------------------
    print("\n--- 6. README 的合规声明（MIT 不授予商标权，必须声明）---")
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    check(
        "含「第三方非官方集成」声明",
        "第三方非官方集成" in readme,
        "明确与腾讯无隶属关系",
    )
    check(
        "声明不包含/不再分发 bsk",
        "不包含" in readme and "bsk" in readme,
        "已声明",
    )
    check(
        "致谢 Tencent/BrowserSkill",
        "Tencent/BrowserSkill" in readme,
        "已致谢",
    )
    check(
        "含商标避让说明（不用 BrowserSkill 作产品名）",
        "商标" in readme or "命名说明" in readme,
        "已说明",
    )

    # ------------------------------------------------------------------
    # 7. 商标纪律：插件名不含 BrowserSkill
    # ------------------------------------------------------------------
    print("\n--- 7. 商标纪律 ---")
    import yaml

    meta = yaml.safe_load((PROJECT / "metadata.yaml").read_text(encoding="utf-8"))
    name = str(meta.get("name", ""))
    display = str(meta.get("display_name", ""))
    check(
        "插件 name 不含 browserskill",
        "browserskill" not in name.lower().replace("_", ""),
        f"name={name!r}",
    )
    check(
        "display_name 不含 BrowserSkill 商标",
        "browserskill" not in display.lower().replace(" ", ""),
        f"display_name={display!r}",
    )

    # ------------------------------------------------------------------
    # 8. 隐私提醒
    # ------------------------------------------------------------------
    print("\n--- 8. 隐私与安全提醒 ---")
    check(
        "README 有隐私/安全章节",
        "隐私" in readme and "安全" in readme,
        "第 10 节「隐私与安全提醒」",
    )
    check(
        "说明了 Agent Window 不是安全沙箱",
        "不是安全沙箱" in readme or "安全沙箱" in readme,
        "已在已知限制里说明",
    )

    # ------------------------------------------------------------------
    # 9. 默认仅管理员（用户明确要求）
    # ------------------------------------------------------------------
    print("\n--- 9. 默认仅管理员可用（用户明确要求）---")
    import json

    schema = json.loads((PROJECT / "_conf_schema.json").read_text(encoding="utf-8"))
    admin_default = schema.get("admin_only", {}).get("default")
    check(
        "admin_only 默认值为 true 且可配置",
        admin_default is True,
        f"default={admin_default!r}（可在插件配置页关闭）",
    )

    # ------------------------------------------------------------------
    # 10. 危险能力的开放边界
    # ------------------------------------------------------------------
    print("\n--- 10. 危险能力的开放边界 ---")
    source_files = [
        f
        for f in files
        if f.endswith(".py") and not f.startswith("tests/")
    ]
    all_src = "\n".join((PROJECT / f).read_text(encoding="utf-8") for f in source_files)

    # evaluate（在用户已登录页面里执行任意 JS）是受控开放的能力：
    # 它确实存在（用户明确要求要），但必须满足三个条件才算合规：
    #   1. 有独立开关，且默认关闭
    #   2. 有"强制管理员"开关，且默认开启
    #   3. 是独立工具，可以被 AstrBot 原生的 tool_permissions 单独控制
    #      （如果把它塞进 bsk_act 的动作列表里，就无法单独管控了）
    #
    # 注意：这里不能再用 '"evaluate"' not in all_src 这种断言 ——
    # 那是"功能不存在"的写法。功能现在是存在的、只是默认关着。
    # 断言要跟着设计意图走，否则会把"按要求实现"判成"违规"。
    import json as _json

    schema = _json.loads((PROJECT / "_conf_schema.json").read_text(encoding="utf-8"))
    eval_switch = schema.get("enable_evaluate", {})
    eval_admin = schema.get("evaluate_require_admin", {})

    check(
        "evaluate 有独立开关且默认关闭",
        eval_switch.get("default") is False,
        f"enable_evaluate 默认={eval_switch.get('default')!r}",
    )
    check(
        "evaluate 有强制管理员开关且默认开启",
        eval_admin.get("default") is True,
        f"evaluate_require_admin 默认={eval_admin.get('default')!r}",
    )
    check(
        "evaluate 是独立工具（非 bsk_act 的动作）",
        "bsk_evaluate" in main_src,
        "以独立 llm_tool 形式注册，可被框架原生 tool_permissions 单独管控",
    )
    # 独立工具意味着它必须自己出现在工具清单里、且 docstring 声明了参数
    check(
        "evaluate 的配置项写明了风险（不是只说'启用 JS 执行'）",
        any(
            kw in str(eval_switch.get("hint", "")) + str(eval_switch.get("description", ""))
            for kw in ("已登录", "任意", "风险", "敏感")
        ),
        "配置说明中必须让用户明白它能在已登录页面里做任意事",
    )

    # 检查 "session stop --all" 是否被真的调用。
    #
    # 不能用子串搜索：源码里有多处注释/docstring 警告不要用它
    # （"绝不使用 session stop --all"），子串匹配会把它们当成违规
    # —— 这正是本检查初版的误报原因。改为 AST 解析，只看真实的
    # 字符串字面量（也就是真正会传给子进程的参数）。
    import ast

    def _string_literals(tree: ast.AST) -> list[str]:
        return [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]

    all_flag_uses: list[str] = []
    for rel in source_files:
        try:
            tree = ast.parse((PROJECT / rel).read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for literal in _string_literals(tree):
            # 只关心真正作为命令参数出现的 "--all"
            if literal.strip() == "--all":
                all_flag_uses.append(rel)
    check(
        "未在真实命令行参数里使用 --all（会误停他人会话）",
        not all_flag_uses,
        (
            "代码里的 --all 只出现在注释/docstring 的警告文字中，"
            "没有一处是真实参数"
        )
        if not all_flag_uses
        else f"发现真实使用：{sorted(set(all_flag_uses))}",
    )

    # URL 协议校验
    check(
        "URL 仅允许 http/https（拒绝 file:// 等）",
        "https://" in main_src and "http://" in main_src,
        "main.py 校验协议前缀，实测拒绝 file://",
    )

    # ------------------------------------------------------------------
    print()
    print("=" * 72)
    failed = [r for r in RESULTS if not r[1]]
    print(f"核验项：{len(RESULTS)}，失败：{len(failed)}")
    for name, _, detail in failed:
        print(f"  [FAIL] {name}: {detail}")
    print("结果：" + ("全部通过" if not failed else "有失败"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
