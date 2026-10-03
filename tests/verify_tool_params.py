"""核验：7 个工具的「签名参数名」与「docstring Args 段」是否一致。

## 为什么需要这个检查
AstrBot 用 **docstring 的 Args 段**生成给模型的参数 schema，
但调用时是按**函数签名的参数名**把值传进来的（`call_local_llm_tool` 里
`handler(event, **tool_args)`）。

如果两者**不一致**，模型按 schema 传参 → 框架按签名找不到对应的形参
→ 抛 `Tool handler parameter mismatch`，用户看到一句英文报错，
而这个 bug 在"直接 await 工具函数"的测试里**完全看不出来**（因为那种
调用绕过了框架的参数注入）。

本次真机实测就是被这类不匹配抓到的（测试里传错了参数名），
所以把它做成一个静态检查，防止将来改 docstring 时两边走偏。

用法：
    python tests/verify_tool_params.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
MAIN = PROJECT / "main.py"

# docstring 里 Args 段支持的类型名（与 AstrBot 的 SUPPORTED_TYPES 对齐）
VALID_TYPES = {"string", "number", "object", "array", "boolean"}
# AstrBot 的 PY_TO_JSON_TYPE 也接受这些 Python 类型名
PY_ALIASES = {"int", "float", "bool", "str", "dict", "list", "tuple", "set"}


def main() -> int:
    print("=" * 72)
    print("工具参数一致性检查（签名 vs docstring schema）")
    print("=" * 72)

    source = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 找出所有被 @filter.llm_tool("name") 装饰的 async def
    tools: list[tuple[str, ast.AsyncFunctionDef]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            fn = deco.func
            if getattr(fn, "attr", "") != "llm_tool":
                continue
            name = (
                deco.args[0].value
                if deco.args and isinstance(deco.args[0], ast.Constant)
                else node.name
            )
            tools.append((name, node))

    print(f"\n找到 {len(tools)} 个 llm_tool\n")

    failures: list[str] = []

    for tool_name, node in sorted(tools, key=lambda t: t[0]):
        doc = ast.get_docstring(node) or ""

        # --- 签名参数（跳过 self 与 event）---
        sig_params = [
            a.arg for a in node.args.args if a.arg not in ("self", "event")
        ]

        # --- docstring 的 Args 段 ---
        doc_args: list[tuple[str, str, str]] = []
        if "Args:" in doc:
            args_block = doc.split("Args:", 1)[1]
            # 形如 `name(type): 描述`；描述可能换行，这里只取本行
            for m in re.finditer(
                r"^\s*(\w+)\((\w+)\)\s*:\s*(.*)$", args_block, re.M
            ):
                doc_args.append((m.group(1), m.group(2), m.group(3).strip()))
        doc_names = [n for n, _, _ in doc_args]

        # --- 比对 ---
        same = sig_params == doc_names
        mark = "ok  " if same else "FAIL"
        print(f"[{mark}] {tool_name}")
        print(f"         签名参数   : {sig_params}")
        print(f"         docstring : {doc_names}")
        if not same:
            missing_in_doc = [p for p in sig_params if p not in doc_names]
            extra_in_doc = [p for p in doc_names if p not in sig_params]
            detail = []
            if missing_in_doc:
                detail.append(f"docstring 缺少 {missing_in_doc}（模型将无法传这些参数）")
            if extra_in_doc:
                detail.append(f"docstring 多出 {extra_in_doc}（模型会传不存在的参数）")
            if sig_params != doc_names and not missing_in_doc and not extra_in_doc:
                detail.append("顺序不一致（AstrBot 按名字匹配，顺序不影响功能，但建议对齐）")
            msg = "；".join(detail) or "不一致"
            print(f"         ★ {msg}")
            failures.append(f"{tool_name}: {msg}")

        # --- 类型名合法性 ---
        for arg_name, arg_type, desc in doc_args:
            if not desc:
                print(f"         ★ {arg_name} 缺少描述（模型不知道这个参数怎么填）")
                failures.append(f"{tool_name}.{arg_name}: 缺少描述")
            if arg_type not in VALID_TYPES and arg_type not in PY_ALIASES:
                print(f"         ★ {arg_name} 的类型名 {arg_type!r} 非法")
                failures.append(f"{tool_name}.{arg_name}: 类型名非法")

    print()
    print("=" * 72)
    if failures:
        print(f"发现问题 {len(failures)} 处：")
        for f in failures:
            print(f"  - {f}")
        print("结果：有失败")
        return 1
    print(f"结果：{len(tools)} 个工具的参数名与 docstring 完全一致，类型名全部合法")
    return 0


if __name__ == "__main__":
    sys.exit(main())
