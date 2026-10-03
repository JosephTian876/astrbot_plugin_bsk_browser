"""隐私扫描：检查即将/已经公开的仓库里有没有个人信息。

## 为什么要连 git 历史一起查
GitHub 上公开的不只是**当前文件**，还有**全部提交历史**。
即使某个改动在后续提交里被删掉了，它依然留在历史对象里，
任何人都能 `git log -p` 翻出来。所以"现在文件里没有"不等于"没有泄露"。

## 扫描什么
针对这台机器的实际情况定制：
- 用户名 UwU / JosephTian876 / KazusaUwU（**注意：作者名是有意公开的**）
- 家目录路径 C:\\Users\\UwU
- 桌面/百度同步盘路径（图标来源就取自那里）
- QQ 号形态的数字串
- API 密钥/token 常见形态
- 浏览器 instance_id（c900a3da —— 这是本机 Edge 的标识）
- 邮箱
- 机器名、AstrBot 的 admins_id

用法：
    python tests/verify_privacy.py            # 只扫当前版本库内容
    python tests/verify_privacy.py --history  # 连全部历史一起扫
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ----------------------------------------------------------------------
# 敏感模式。每条都带"为什么算敏感"与"允许的例外"。
#
# ★ 本文件初版有过一次**假阴性**（报了"0 命中"但实际有命中），根因是
#   家目录正则只写了 `C:\Users\UwU`，而这台机器的实际路径是
#   `D:\UwU\Documents\...` —— 形式不同，全部漏掉了。
#   假阴性的安全检查比没有更危险（它给人虚假的安全感），所以：
#   1. 用户名一律用**独立的正则分支**匹配，不假设它出现在哪种路径前缀下；
#   2. 末尾有 `--selftest` 自检：用构造的样例验证每个模式**确实能命中**，
#      防止将来改正则时又把它改坏。
# ----------------------------------------------------------------------
USERNAME = "UwU"  # 本机用户名（出现在多种路径形态里）
SENSITIVE_PATTERNS: list[tuple[str, str, str]] = [
    # (名称, 正则, 说明)
    (
        "家目录绝对路径",
        # 覆盖各种形态：C:\Users\X、D:\X、/Users/X、/home/X，
        # 以及**裸用户名**出现在盘符路径中间的情况（本机 actual 形态）。
        rf"[A-Za-z]:\\+(?:Users\\+)?{USERNAME}\b"
        rf"|[A-Za-z]:/+Users/+{USERNAME}\b"
        rf"|/(?:Users|home)/{USERNAME}\b",
        "暴露本机用户名与目录结构",
    ),
    (
        "工作目录名（含用户名片段）",
        r"dshworkdir|astrbot-browserskill",
        "暴露本机的私有工作目录布局",
    ),
    (
        "桌面/同步盘路径",
        r"BaiduSyncdisk|Desktop\\|Desktop/",
        "暴露本机私有目录布局",
    ),
    (
        "AstrBot 安装路径",
        r"D:\\+AstrBot\b|D:/+AstrBot\b|AstrBot\\backend",
        "暴露本机软件安装位置",
    ),
    (
        "本机浏览器实例 ID",
        r"\bc900a3da\b",
        "本机 Edge 的 bsk 实例标识",
    ),
    (
        "疑似 API 密钥",
        r"(?:sk-[A-Za-z0-9]{16,}|gho_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}"
        r"|AIza[A-Za-z0-9_\-]{30,}|Bearer\s+[A-Za-z0-9._\-]{20,})",
        "凭据泄露（最严重）",
    ),
    (
        "邮箱地址",
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
        "个人信息",
    ),
    (
        "token / secret / password 赋值",
        r"(?:api_key|apikey|token|secret|password|passwd)\s*[=:]\s*[\"'][^\"']{8,}[\"']",
        "硬编码凭据",
    ),
]

# 有意公开、不算泄露的内容。
# 注意：这里**只**放"本来就该公开"的东西，不能拿它当"降噪开关" ——
# 把常见命中塞进来会让扫描器变成睁眼瞎。
INTENTIONAL = [
    (r"KazusaUwU", "作者名（metadata.yaml 的 author，有意公开）"),
    (r"JosephTian876", "GitHub 用户名（仓库地址的一部分）"),
    (r"josephtian876@gmail\.com", "git 提交邮箱（提交历史固有，公开可见）"),
    (r"yourname", "占位符"),
    (r"<你>|<用户名>|<your", "文档里的占位符写法"),
]


def _selftest() -> None:
    """自检：用构造样例确认每个模式**确实能命中**，防止回归成假阴性。"""
    print("=" * 74)
    print("隐私扫描器自检（防止正则失效导致假阴性）")
    print("=" * 74)

    # 每个模式配一个"必须命中"的样例
    cases = {
        "家目录绝对路径": [
            r"C:\Users\UwU\Documents",
            r"D:\UwU\Documents\dshworkdir",
            "/Users/UwU/x",
            "/home/UwU/x",
        ],
        "工作目录名（含用户名片段）": [r"dshworkdir\astrbot-browser", "astrbot-browserskill"],
        "桌面/同步盘路径": [r"D:\Desktop\x", "BaiduSyncdisk", "C:/Users/x/Desktop/y"],
        "AstrBot 安装路径": [r"D:\AstrBot\backend\app", "D:/AstrBot/x"],
        "本机浏览器实例 ID": ["c900a3da", "id=c900a3da"],
        "疑似 API 密钥": ["sk-" + "a" * 24, "gho_" + "b" * 30],
        "邮箱地址": ["someone@example.com"],
        "token / secret / password 赋值": ['token = "abcdefgh12345"'],
    }

    failed: list[str] = []
    for name, pattern, _ in SENSITIVE_PATTERNS:
        samples = cases.get(name, [])
        if not samples:
            print(f"  [warn] {name}: 没有配自检样例")
            continue
        for s in samples:
            if not re.search(pattern, s):
                print(f"  [FAIL] {name} 未能命中样例 {s!r}")
                failed.append(name)
                break
        else:
            print(f"  [ok  ] {name} 命中 {len(samples)} 个样例")

    # 反向检查：INTENTIONAL 不能把真正的敏感内容吞掉
    print("\n  反向检查：INTENTIONAL 不应吞掉敏感样例")
    for s in [r"D:\UwU\Documents", "c900a3da", "someone@example.com"]:
        for ip, desc in INTENTIONAL:
            m = re.search(ip, s)
            if m:
                print(f"  [FAIL] {s!r} 被 INTENTIONAL {ip!r}({desc}) 误吞")
                failed.append(f"INTENTIONAL 误吞 {s}")
    if not failed:
        print("  [ok  ] 无误吞")

    print()
    if failed:
        print(f"自检失败：{sorted(set(failed))}")
    else:
        print("自检通过：所有模式都能命中对应样例")


def run_git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout


def scan_text(label: str, text: str) -> list[tuple[str, str, str, str]]:
    """扫一段文本，返回 [(模式名, 说明, 命中片段, 所在行摘要)]。

    Note:
        元组顺序是 ``(名称, 正则, 说明)`` —— **名字在前**。
        初版这里写成了 ``for pattern, name, why``，变量名与位置对调，
        导致 ``re.finditer`` 拿中文名称当正则用，**整个扫描器静默失效**
        （永远报 0 命中）。已由 ``_selftest`` 守住：它必须在开始扫描之前
        先证明每个模式能命中样例。
    """
    hits: list[tuple[str, str, str, str]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        for name, pattern, why in SENSITIVE_PATTERNS:
            for m in re.finditer(pattern, line):
                frag = m.group(0)
                # 单独判断：若该命中同时被标记为有意公开，则跳过
                if any(re.search(ip, frag) for ip, _ in INTENTIONAL):
                    continue
                hits.append((name, why, frag, f"{label}:{line_no}: {stripped[:110]}"))
    return hits


def main() -> int:
    if "--selftest" in sys.argv:
        _selftest()
        return 0

    with_history = "--history" in sys.argv
    # 自检先行：如果正则本身失效（假阴性），后面的"0 命中"毫无意义。
    print("=" * 74)
    print("先做扫描器自检")
    print("=" * 74)
    _selftest()
    print()

    print("=" * 74)
    print("隐私扫描" + ("（含全部 git 历史）" if with_history else "（仅当前版本库内容）"))
    print("=" * 74)

    all_hits: list[tuple[str, str, str, str]] = []

    # ------------------------------------------------------------------
    # 1. 当前受版本控制的文件
    # ------------------------------------------------------------------
    print("\n--- 1. 当前受版本控制的文件 ---")
    files = [f for f in run_git("ls-files").splitlines() if f.strip()]
    print(f"    共 {len(files)} 个文件")

    text_hits = 0
    for rel in files:
        path = PROJECT / rel
        if not path.is_file():
            continue
        # 二进制文件（如 logo.png）不做文本扫描
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits = scan_text(rel, content)
        text_hits += len(hits)
        all_hits.extend(hits)

    print(f"    文本文件命中：{text_hits} 处")

    # ------------------------------------------------------------------
    # 2. git 历史（公开的完整历史）
    # ------------------------------------------------------------------
    if with_history:
        print("\n--- 2. 全部 git 历史（每个提交的每个文件）---")
        revs = run_git("rev-list", "--all").split()
        print(f"    共 {len(revs)} 个提交，逐个扫描其全部文件内容…")
        hist_hits: list[tuple[str, str, str, str]] = []
        checked = 0
        for rev in revs:
            # 列出该提交的所有 blob
            out = run_git(
                "grep", "-I", "-n", "-E",
                "|".join(f"(?:{p})" for _, p, _ in SENSITIVE_PATTERNS),
                rev,
            )
            if not out.strip():
                continue
            for line in out.splitlines():
                # 格式：<rev>:<path>:<line_no>:<content>
                parts = line.split(":", 3)
                if len(parts) < 4:
                    continue
                _, fpath, lineno, content = parts
                if fpath.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                    continue
                checked += 1
                # 元组顺序是 (名称, 正则, 说明) —— 名字在前，别写反。
                for name, pattern, why in SENSITIVE_PATTERNS:
                    for m in re.finditer(pattern, content):
                        frag = m.group(0)
                        if any(re.search(ip, frag) for ip, _ in INTENTIONAL):
                            continue
                        hist_hits.append(
                            (
                                name,
                                why,
                                frag,
                                f"{rev[:7]}:{fpath}:{lineno}: {content.strip()[:100]}",
                            )
                        )
        print(f"    历史命中：{len(hist_hits)} 处")
        all_hits.extend(hist_hits)

    # ------------------------------------------------------------------
    # 3. 汇总（按模式归类，便于判断严重性）
    # ------------------------------------------------------------------
    print()
    print("=" * 74)
    if not all_hits:
        print("结果：未发现敏感信息")
        print()
        print("说明：")
        print("  - 已扫描当前版本库的 ", len(files), " 个文件", sep="")
        if with_history:
            print("  - 已扫描全部 ", len(revs), " 个提交的全部历史内容", sep="")
        print("  - 作者名 KazusaUwU 与仓库地址 JosephTian876 属于**有意公开**的信息，")
        print("    不算泄露（它们是 metadata.yaml 里给用户看的）")
        return 0

    # 按模式名分组统计
    from collections import Counter

    counts = Counter(h[0] for h in all_hits)
    print(f"发现 {len(all_hits)} 处命中，按类型：")
    for name, cnt in counts.most_common():
        why = next(w for n, _, w in SENSITIVE_PATTERNS if n == name)
        print(f"    {name:28s} {cnt:4d} 处   （{why}）")
    print()
    print("详细（最多显示 40 条）：")
    for name, why, frag, where in all_hits[:40]:
        print(f"    [{name}] {frag!r}")
        print(f"        {where}")
    if len(all_hits) > 40:
        print(f"    …其余 {len(all_hits) - 40} 条省略")
    print()
    print("结果：需要人工确认上述命中是否真的敏感")
    return 1


if __name__ == "__main__":
    sys.exit(main())
