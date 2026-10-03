"""一致性校验：config.py 的默认值 vs _conf_schema.json 的默认值。

两份默认值必须完全一致，否则用户会在 WebUI 里看到一个值、插件实际用另一个值。
这是发布前必须过的检查项，所以做成可重复执行的脚本而不是一次性命令。
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from bsk.config import parse_settings  # noqa: E402


def main() -> int:
    schema_path = PROJECT / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    settings = parse_settings({})

    print("=" * 66)
    print("配置默认值一致性校验")
    print("=" * 66)

    mismatches: list[str] = []
    missing: list[str] = []

    for key, spec in schema.items():
        schema_default = spec.get("default")
        if not hasattr(settings, key):
            missing.append(key)
            print(f"[MISS] {key:24s} schema 里有，Settings 里没有")
            continue

        parsed = getattr(settings, key)
        # JSON 没有 tuple，列表类字段要归一化后比较。
        if isinstance(parsed, tuple):
            parsed = list(parsed)
        if isinstance(schema_default, (int, float)) and isinstance(parsed, (int, float)):
            same = float(schema_default) == float(parsed)
        else:
            same = schema_default == parsed

        mark = "ok  " if same else "DIFF"
        if not same:
            mismatches.append(key)
        print(f"[{mark}] {key:24s} schema={schema_default!r:22s} 实际={parsed!r}")

    # 反向检查：Settings 有但 schema 没有（用户无法配置，可能是遗漏）
    # 注意 Settings 是 slots=True 的 dataclass，没有 __dict__，必须用 fields()。
    field_names = {f.name for f in dataclasses.fields(settings)}
    extra = sorted(field_names - set(schema))

    print()
    if missing:
        print(f"Settings 缺失字段：{missing}")
    if extra:
        print(f"schema 未暴露的字段：{extra}（如果是有意不暴露，可忽略）")
    if mismatches:
        print(f"默认值不一致：{mismatches}")
        return 1

    print("结果：" + f"{len(schema)} 项默认值完全一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
