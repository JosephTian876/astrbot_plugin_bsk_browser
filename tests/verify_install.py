"""安装烟雾测试：模拟用户第一次装插件的完整流程。

验证的是**别人拿到这个仓库后能不能用起来**，而不是我本机能不能跑：
1. 从一个干净的副本安装（排除开发产物），确认没有"只在源目录才能跑"的隐含依赖；
2. 目录结构符合 AstrBot 发现规则（main.py 在插件目录根）；
3. 用 AstrBot 的加载方式 import，必需工具全部注册（并拒绝意料之外的工具名）；
4. 实例化 + 生命周期可用；
5. 不残留任何运行期目录（尤其不能生成 data/）。

这一步能抓住一类真实问题：文件漏提交、依赖了只在开发机上存在的路径、
把 tests/ 里的东西当成了运行必需。

用法：
    python tests/verify_install.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
ASTRBOT_APP = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    print("=" * 72)
    print("安装烟雾测试：模拟用户首次安装")
    print("=" * 72)

    # --- 1. 用 git 的已跟踪文件列表来复制 ---
    # 这是最贴近"用户 clone 仓库"的方式：只复制会被发布的内容，
    # 开发机上存在但没提交的文件不该出现在安装结果里。
    print("\n--- 1. 从版本库导出干净副本 ---")
    try:
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.splitlines()
        tracked = [t.strip() for t in tracked if t.strip()]
        check("读取版本库文件列表", bool(tracked), f"{len(tracked)} 个文件")
    except Exception as exc:  # noqa: BLE001
        check("读取版本库文件列表", False, repr(exc))
        return 1

    tmp_root = tempfile.mkdtemp(prefix="bsk-install-smoke-")
    plugin_dir = Path(tmp_root) / "astrbot_plugin_bsk_browser"
    plugin_dir.mkdir(parents=True)

    for rel in tracked:
        src = PROJECT / rel
        if not src.is_file():
            continue
        dst = plugin_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    check(
        "复制完成",
        (plugin_dir / "main.py").is_file(),
        f"{sum(1 for _ in plugin_dir.rglob('*') if _.is_file())} 个文件",
    )

    # --- 2. 结构符合 AstrBot 的发现规则 ---
    print("\n--- 2. 目录结构（AstrBot 发现规则）---")
    # star_manager._get_modules：目录下有 main.py 就用它作入口
    check("插件根有 main.py", (plugin_dir / "main.py").is_file(), "否则插件不会被发现")
    check("插件根有 metadata.yaml", (plugin_dir / "metadata.yaml").is_file(), "")
    check("插件根有 _conf_schema.json", (plugin_dir / "_conf_schema.json").is_file(), "")
    check(
        "插件根没有 __init__.py",
        not (plugin_dir / "__init__.py").exists(),
        "插件是数据目录不是 Python 包，多一个 __init__.py 会让 AstrBot 的 import 路径混乱",
    )
    check(
        "bsk/ 子包有 __init__.py",
        (plugin_dir / "bsk" / "__init__.py").is_file(),
        "",
    )
    # 开发产物不该出现在安装结果里
    junk = [
        str(p.relative_to(plugin_dir))
        for p in plugin_dir.rglob("*")
        if p.is_dir() and p.name in {"__pycache__", ".git", ".pytest_cache"}
    ]
    check("不含开发产物", not junk, f"发现：{junk}" if junk else "")

    # --- 3. 在干净副本上跑真实 AstrBot 加载 ---
    print("\n--- 3. 在干净副本上加载（真实 AstrBot）---")
    # 把副本放进一个临时的 data/plugins 结构，完全复刻 AstrBot 的 import 路径
    fake_root = Path(tmp_root) / "astrbot_root"
    plugins_dir = fake_root / "data" / "plugins"
    plugins_dir.mkdir(parents=True)
    installed = plugins_dir / "astrbot_plugin_bsk_browser"
    shutil.copytree(plugin_dir, installed)

    probe = f'''
import sys
sys.path.insert(0, r"{ASTRBOT_APP}")
sys.path.insert(0, r"{fake_root}")
import json

out = {{"ok": False}}
try:
    mod = __import__("data.plugins.astrbot_plugin_bsk_browser.main", fromlist=["main"])
    from astrbot.core.provider.register import llm_tools
    names = sorted(t.name for t in llm_tools.func_list if t.name.startswith("bsk_"))
    cls = mod.BskBrowserPlugin

    # 硬约束
    checks = {{
        "tools": names,
        "no_del": "__del__" not in cls.__dict__,
        "has_terminate": "terminate" in cls.__dict__,
        "has_initialize": "initialize" in cls.__dict__,
    }}

    # 实例化 + 生命周期（不碰浏览器）
    import asyncio
    sys.path.insert(0, r"{PROJECT / 'tests'}")
    from astrbot_test_doubles import make_context
    inst = cls(make_context(), config={{"enabled": True, "admin_only": True}})

    async def life():
        await inst.initialize()
        await inst.terminate()
    asyncio.run(life())
    checks["lifecycle"] = True

    out = {{"ok": True, "checks": checks}}
except Exception as exc:
    import traceback
    out = {{"ok": False, "error": traceback.format_exc()}}

print("__RESULT__" + json.dumps(out, ensure_ascii=False))
'''

    # 关键：CWD 必须是 fake_root，让 AstrBot 把它当作 root（这样不会污染项目目录）
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(fake_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
    )

    payload = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("__RESULT__"):
            payload = json.loads(line[len("__RESULT__") :])
            break

    if payload is None:
        check(
            "干净副本可加载",
            False,
            f"没有拿到结果。stdout 尾部：{(proc.stdout or '')[-400:]}\nstderr 尾部：{(proc.stderr or '')[-400:]}",
        )
    elif not payload.get("ok"):
        check("干净副本可加载", False, payload.get("error", "")[-500:])
    else:
        c = payload["checks"]
        check("干净副本可加载", True, "")
        # ★ 必须存在的工具（少一个就是回归）。
        required = {
            "bsk_open",
            "bsk_read",
            "bsk_act",
            "bsk_screenshot",
            "bsk_close",
            "bsk_status",
        }
        # 允许存在的额外工具（新增功能会加工具，不该让这个断言失败）。
        #
        # 为什么不用 `set(tools) == required`：那样的精确相等断言会在**每次
        # 新增工具**时失败，把正常的功能演进报成回归（本文件初版就是如此，
        # 加了 bsk_evaluate 之后必然误报）。但也**不能**只写成子集判断 ——
        # 那样工具名写错（例如 bsk_clsoe）就检测不到了。
        # 所以两边都查：required 必须齐全，且不能出现意料之外的名字。
        allowed = required | {"bsk_evaluate"}
        actual = set(c["tools"])
        check(
            "6 个必需工具全部注册",
            required.issubset(actual),
            f"缺少：{sorted(required - actual)}" if not required.issubset(actual) else f"实际：{sorted(actual)}",
        )
        check(
            "无意料之外的工具名（改名/拼错会被抓到）",
            actual.issubset(allowed),
            f"多出：{sorted(actual - allowed)}" if not actual.issubset(allowed) else "",
        )
        check("无 __del__（C3）", c["no_del"], "")
        check("有 terminate（C4）", c["has_terminate"], "")
        check("有 initialize（C4）", c["has_initialize"], "")
        check("生命周期可运行", c.get("lifecycle", False), "")

    # --- 4. 不能污染工作目录 ---
    print("\n--- 4. 运行期污染检查 ---")
    leftovers = sorted(
        p.name for p in Path(tmp_root).iterdir() if p.is_dir()
    )
    check(
        "未在插件目录生成 data/ 等运行期目录",
        not (installed / "data").exists(),
        f"生成于 {installed / 'data'}" if (installed / "data").exists() else "",
    )
    check(
        "未在插件目录生成截图目录",
        not (installed / "shots").exists(),
        "",
    )

    # --- 清理 ---
    shutil.rmtree(tmp_root, ignore_errors=True)

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
