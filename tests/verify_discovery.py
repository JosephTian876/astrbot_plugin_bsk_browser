"""真实发现路径验证：AstrBot 自己能不能"找到并加载"这个插件。

已有的 L2 契约测试是**我手动拼 import 路径**去加载插件；这里不同 ——
它调用 AstrBot **自己的插件发现函数**（``PluginManager._get_modules``），
然后走它自己的元数据解析与版本校验流程。

为什么值得单独测：发现逻辑有一堆隐式规则（目录下必须有 main.py、
跳过 ``.plugin-install-*`` 之类前缀、``_conf_schema.json`` 决定是否给配置…），
任何一条不满足，插件在用户机器上就是**装了但不出现**。
这类问题在我手动 import 的测试里完全看不出来。

用法：
    python tests/verify_discovery.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

ASTRBOT_APP = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
if os.path.isdir(ASTRBOT_APP):
    sys.path.insert(0, ASTRBOT_APP)

# ★ 关键：把 AstrBot 的 root 钉到用户真实目录，并让 CWD 离开项目目录。
#
# 原因：AstrBot 解析数据路径时优先读 ``ASTRBOT_ROOT``，否则桌面版用
# ``~/.astrbot``、普通模式用 **当前工作目录**（astrbot_path.py:29-35）。
# 如果本脚本在项目根目录下运行且不设置该变量，AstrBot 会把项目目录当成
# root，就地生成 ``data/cmd_config.json``（含 AstrBot 主配置）——
# 这正是之前误提交 data/ 目录的成因。
os.environ.setdefault(
    "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    print("=" * 72)
    print("真实发现路径验证：AstrBot 能否找到并加载本插件")
    print("=" * 72)

    plugin_dir = Path(os.path.expanduser("~")) / ".astrbot" / "data" / "plugins"
    our_dir = plugin_dir / "astrbot_plugin_bsk_browser"

    print(f"\n插件目录：{our_dir}")
    check("插件已安装到 AstrBot 插件目录", our_dir.is_dir(), str(our_dir) if our_dir.is_dir() else "不存在")

    if not our_dir.is_dir():
        print("\n请先把插件复制到该目录再运行本脚本。")
        return 1

    # ------------------------------------------------------------------
    # 1. 用 AstrBot 自己的发现函数（不是我们手写 glob）
    # ------------------------------------------------------------------
    print("\n--- 1. AstrBot 的插件发现（_get_modules）---")
    try:
        from astrbot.core.star.star_manager import PluginManager

        modules = PluginManager._get_modules(str(plugin_dir))
        found = [m for m in modules if m.get("pname") == "astrbot_plugin_bsk_browser"]
        check(
            "AstrBot 发现了本插件",
            bool(found),
            f"发现的插件总数={len(modules)}，本插件={'找到' if found else '未找到'}",
        )
        if found:
            entry = found[0]
            check(
                "入口模块名为 main",
                entry.get("module") == "main",
                f"module={entry.get('module')!r}（AstrBot 只认 main.py 或与目录同名的 .py）",
            )
            expected_path = str(our_dir / "main")
            check(
                "模块路径指向 main.py",
                str(entry.get("module_path")) == expected_path,
                f"实际={entry.get('module_path')!r}",
            )
    except Exception as exc:  # noqa: BLE001
        import traceback

        check("AstrBot 的插件发现（_get_modules）", False, traceback.format_exc()[-400:])

    # ------------------------------------------------------------------
    # 2. AstrBot 自己的元数据解析
    # ------------------------------------------------------------------
    print("\n--- 2. AstrBot 的元数据解析（_load_plugin_metadata）---")
    try:
        meta = PluginManager._load_plugin_metadata(str(our_dir))
        check("解析出元数据", meta is not None, "")
        if meta is not None:
            check("name 正确", meta.name == "astrbot_plugin_bsk_browser", f"name={meta.name!r}")
            check("author 非空", bool(str(meta.author).strip()), f"author={meta.author!r}")
            check("desc 非空", bool(str(meta.desc).strip()), f"desc 长度={len(str(meta.desc))}")
            check("version 非空", bool(str(meta.version).strip()), f"version={meta.version!r}")
            check(
                "astrbot_version 已声明",
                bool(str(meta.astrbot_version).strip()),
                f"astrbot_version={meta.astrbot_version!r}",
            )
            # 注意：这里**不**断言 star_cls_type 非空。
            # 它由插件 import 时的 Star.__init_subclass__ 填充（star/base.py:64），
            # 而 _load_plugin_metadata 只读 YAML、不做 import，所以此处为 None
            # 是**正常**的。插件类是否真的被注册，由第 6 步用真实 import 验证。
            print(
                "       （star_cls_type 在此为 None 属正常：该函数只读 YAML 不 import，"
                "插件类由第 6 步验证）"
            )
    except Exception as exc:  # noqa: BLE001
        import traceback

        check("AstrBot 的元数据解析", False, traceback.format_exc()[-400:])

    # ------------------------------------------------------------------
    # 3. AstrBot 的版本兼容校验（装到不兼容版本上会被它拒绝）
    # ------------------------------------------------------------------
    print("\n--- 3. 版本兼容校验 ---")
    try:
        import astrbot

        current = getattr(astrbot, "__version__", "unknown")
        version_spec = str(getattr(meta, "astrbot_version", "") or "")
        if version_spec:
            ok = PluginManager._validate_astrbot_version_specifier(version_spec)
            check(
                f"声明 {version_spec} 对当前 AstrBot {current} 兼容",
                bool(ok),
                "" if ok else "用户装到当前版本会被 AstrBot 拒绝加载",
            )
        else:
            check("声明了 astrbot_version", False, "未声明会导致不做版本检查")
    except Exception as exc:  # noqa: BLE001
        check("版本兼容校验", False, repr(exc))

    # ------------------------------------------------------------------
    # 4. 配置 schema 能被 AstrBot 解析成配置对象
    # ------------------------------------------------------------------
    print("\n--- 4. 配置 schema 被 AstrBot 接受 ---")
    try:
        import json
        import tempfile

        from astrbot.core.config.astrbot_config import AstrBotConfig

        schema = PluginManager._load_plugin_config_schema(
            str(our_dir / "_conf_schema.json")
        )
        check("schema 可被解析", isinstance(schema, dict), f"{len(schema)} 项")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AstrBotConfig(
                config_path=os.path.join(tmp, "cfg.json"), schema=schema
            )
            check(
                "AstrBot 能据 schema 生成配置对象",
                cfg is not None,
                f"生成 {len(cfg)} 个键（非法 type 会在这里抛 TypeError）",
            )
    except Exception as exc:  # noqa: BLE001
        import traceback

        check("配置 schema 被 AstrBot 接受", False, traceback.format_exc()[-400:])

    # ------------------------------------------------------------------
    # 6. 真实 import 后，Star 子类是否被 AstrBot 的注册表捕获
    # ------------------------------------------------------------------
    # 这一步才真正回答"插件类有没有被框架认出来"：Star.__init_subclass__
    # 会在 import 时把子类塞进 star_map / star_registry。
    print("\n--- 6. import 后 Star 子类注册情况 ---")
    try:
        home = os.path.expanduser("~")
        root = os.path.join(home, ".astrbot")
        for p in (root,):
            if os.path.isdir(p) and p not in sys.path:
                sys.path.insert(0, p)

        from astrbot.core.star.star import star_map

        mod = __import__(
            "data.plugins.astrbot_plugin_bsk_browser.main", fromlist=["main"]
        )
        module_path = mod.__name__
        registered = star_map.get(module_path)
        check(
            "import 后 star_map 里有本插件",
            registered is not None,
            f"key={module_path!r}",
        )
        if registered is not None:
            check(
                "star_cls_type 已填充为插件类",
                registered.star_cls_type is mod.BskBrowserPlugin,
                f"star_cls_type={registered.star_cls_type}",
            )
    except Exception as exc:  # noqa: BLE001
        import traceback

        check("import 后 Star 子类注册情况", False, traceback.format_exc()[-400:])

    # ------------------------------------------------------------------
    # 7. 插件目录里不该有会被误认成插件的东西
    # ------------------------------------------------------------------
    print("\n--- 7. 目录卫生 ---")
    stray = [
        p.name
        for p in our_dir.iterdir()
        if p.is_dir() and not p.name.startswith((".", "__")) and p.name not in {"bsk", "tests"}
    ]
    # ``data/`` 是**运行期产物**，不是插件源码：AstrBot 解析数据路径时用
    # 「当前工作目录」当 root（astrbot_path.py:35），任何在插件目录下
    # import astrbot 的脚本都会就地生成一个 data/（内含 AstrBot 主配置）。
    # 它不该出现在安装目录里，发现即清理。
    runtime_dirs = {"data", "shots", "runtime"}
    junk_runtime = [d for d in stray if d in runtime_dirs]
    if junk_runtime:
        import shutil

        for d in junk_runtime:
            shutil.rmtree(our_dir / d, ignore_errors=True)
        print(f"       已清理运行期产物：{junk_runtime}")
        stray = [d for d in stray if d not in runtime_dirs]

    check(
        "没有多余的会被误认的顶层目录",
        not stray,
        f"发现：{stray}（AstrBot 只扫描 data/plugins 的直接子目录，这通常无害，但值得留意）",
    )
    # AstrBot 跳过这些前缀的目录
    suspicious = [
        d.name for d in plugin_dir.iterdir() if d.name.startswith((".plugin-install-", ".plugin-upload-"))
    ]
    if suspicious:
        print(f"       （提示：插件目录下存在被 AstrBot 跳过的临时目录：{suspicious}）")

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
