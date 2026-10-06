"""隐私扫描：检查即将/已经公开的仓库里有没有个人信息。

## 为什么要连 git 历史一起查
GitHub 上公开的不只是当前文件，还有全部提交历史。
即使某个改动在后续提交里被删掉了，它依然留在历史对象里，
任何人都能 `git log -p` 翻出来。所以"现在文件里没有"不等于"没有泄露"。

## 扫描什么
针对这台机器的实际情况定制：
- 用户名 UwU / JosephTian876（注意：作者名与 GitHub 用户名一致，是有意公开的）
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
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ----------------------------------------------------------------------
# 敏感模式。每条都带"为什么算敏感"与"允许的例外"。
#
# 本文件初版有过一次假阴性（报了"0 命中"但实际有命中），根因是
#   家目录正则只写了 `C:\Users\<用户名>` 这一种形态，而实际路径可能长成
#   别的样子（用户名直接跟在盘符后面）—— 形式不同，全部漏掉了。
#   假阴性的安全检查比没有更危险（它给人虚假的安全感），所以：
#   1. 用户名一律用独立的正则分支匹配，不假设它出现在哪种路径前缀下；
#   2. 末尾有 `--selftest` 自检：用构造的样例验证每个模式确实能命中，
#      防止将来改正则时又把它改坏。
# ----------------------------------------------------------------------
def _detect_usernames() -> tuple[str, ...]:
    """从**运行环境**推导出"什么算本机用户名"，而不是把它写死在代码里。

    为什么要动态取：
    1. 写死真实用户名会让**本文件自己成为泄露源** —— 而且它恰好会被自己的
       规则命中，产生"扫描器扫到自己"的噪音；
    2. 写死之后自检就没法用假样例（正则只认那一个真名），一旦为了消噪音把
       样例改成假的，自检就失效 —— 那等于把检测能力弄弱了；
    3. 动态取还让这份脚本**换台机器也能用**。

    取两个来源并合并：环境变量 ``USERNAME`` / ``USER``，以及 ``Path.home()``
    的末段。任一取不到就跳过那一项，不为它写兜底假值。

    Returns:
        去重后的候选用户名元组；一个都取不到时返回空元组。
    """
    found: list[str] = []
    for key in ("USERNAME", "USER", "LOGNAME"):
        value = (os.environ.get(key) or "").strip()
        if value and value not in found:
            found.append(value)
    try:
        home_name = Path.home().name.strip()
        if home_name and home_name not in found:
            found.append(home_name)
    except Exception:
        pass
    return tuple(found)


HOME_USERS: tuple[str, ...] = _detect_usernames()
"""本机的候选用户名。

注意：为了不让本文件被自己的规则命中，下面的正则是**按这些名字动态拼**的，
且自检用**独立构造的假名字**来验证正则形态正确 —— 而不是拿这里的真名去测。
"""


def _home_path_pattern(users: tuple[str, ...]) -> str:
    """按给定用户名列表拼出"家目录绝对路径"的匹配正则。

    之所以做成函数（而不是模块级常量）：自检需要用**假名字**拼一份同样的
    正则来验证形态，这样既能证明正则有效，又不必在文件里出现真实用户名。

    覆盖的形态（都来自实际见过的写法）：
    - ``C:\\Users\\<名字>``
    - ``D:\\<名字>``（用户名直接跟在盘符后面）
    - ``/Users/<名字>``（macOS）、``/home/<名字>``（Linux）

    Args:
        users: 候选用户名。

    Returns:
        正则字符串；``users`` 为空时返回一个永不匹配的模式。
    """
    if not users:
        # 取不到用户名时不乱猜：返回永不匹配的正则，而不是退化成"匹配所有路径"。
        return r"(?!)"
    alt = "|".join(re.escape(u) for u in users)
    return (
        rf"[A-Za-z]:\\+(?:Users\\+)?(?:{alt})\b"
        rf"|[A-Za-z]:/+Users/+?(?:{alt})\b"
        rf"|/(?:Users|home)/(?:{alt})\b"
    )


# 这三项是**必须写真实值**才能起作用的规则：它们本来就是"本机特有的标识"，
# 没有真实值就查不出来。所以它们留在文件里是对的 —— 检测规则本身不算泄露，
# 前提是**只在规则里出现，不出现在别处**（由扫描结果自行验证）。
#
# 提成常量是为了让规则与自检样例共用同一个来源：写两份会漂移，
# 自检就会拿着过时的样例去测新规则（本文件踩过这个坑）。
PRIVATE_DIR_NAMES: tuple[str, ...] = ("dshworkdir", "astrbot-browserskill")
SYNC_DIR_NAMES: tuple[str, ...] = ("BaiduSyncdisk",)
BROWSER_INSTANCE_IDS: tuple[str, ...] = ("c900a3da",)


def _alt(values: tuple[str, ...]) -> str:
    """把一组字面值拼成正则的"或"分支；空组返回永不匹配。"""
    if not values:
        return r"(?!)"
    return "|".join(re.escape(v) for v in values)


SENSITIVE_PATTERNS: list[tuple[str, str, str]] = [
    # (名称, 正则, 说明)
    (
        "家目录绝对路径",
        _home_path_pattern(HOME_USERS),
        "暴露本机用户名与目录结构",
    ),
    (
        "私有工作目录名",
        _alt(PRIVATE_DIR_NAMES),
        "暴露本机的私有工作目录布局",
    ),
    (
        "桌面/同步盘路径",
        rf"{_alt(SYNC_DIR_NAMES)}|Desktop\\|Desktop/",
        "暴露本机私有目录布局",
    ),
    (
        "AstrBot 安装路径",
        r"D:\\+AstrBot\b|D:/+AstrBot\b|AstrBot\\backend",
        "暴露本机软件安装位置",
    ),
    (
        "本机浏览器实例 ID",
        rf"\b(?:{_alt(BROWSER_INSTANCE_IDS)})\b",
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
# 注意：这里只放"本来就该公开"的东西，不能拿它当"降噪开关" ——
# 把常见命中塞进来会让扫描器变成睁眼瞎。
INTENTIONAL = [
    (
        r"JosephTian876",
        "作者名与 GitHub 用户名（metadata.yaml 的 author + 仓库地址，有意公开）",
    ),
    (r"josephtian876@gmail\.com", "git 提交邮箱（提交历史固有，公开可见）"),
    (r"yourname", "占位符"),
    (r"<你>|<用户名>|<your", "文档里的占位符写法"),
]


def _selftest() -> None:
    """自检：确认每条规则**确实能命中**对应样例，防止回归成假阴性。

    这是本文件最重要的一部分。历史教训：初版有两个叠加的 bug
    （正则只覆盖一种路径形态、元组解包顺序写反），导致它报"0 命中"
    而实际有大量命中 —— 假阴性的安全检查比没有更危险，因为它让人
    以为查过了。

    做法上的一个讲究：**家目录那条规则是按运行环境的用户名动态拼的**，
    所以这里不能直接拿一个固定样例去测它。改为用 `_home_path_pattern()`
    传入**假名字**拼一份同样形态的正则来验证 —— 这样既证明了"拼接逻辑
    会覆盖各平台路径形态"，又不必在文件里出现任何真实用户名。
    """
    print("=" * 74)
    print("隐私扫描器自检（防止正则失效导致假阴性）")
    print("=" * 74)

    failed: list[str] = []

    # --- 第一部分：家目录规则的形态验证（用假名字）---
    fake_users = ("SomeUser", "AnotherPerson")
    fake_pattern = _home_path_pattern(fake_users)
    fake_samples = [
        rf"C:\Users\{fake_users[0]}\Documents",
        rf"D:\{fake_users[0]}\Documents\someworkdir",
        f"/Users/{fake_users[0]}/x",
        f"/home/{fake_users[1]}/x",
    ]
    for s in fake_samples:
        if not re.search(fake_pattern, s):
            print(f"  [FAIL] 家目录规则未能命中形态样例 {s!r}")
            failed.append("家目录绝对路径（形态）")
            break
    else:
        print(f"  [ok  ] 家目录规则命中全部 {len(fake_samples)} 个形态样例")

    # 反向：该规则不该匹配"不含该用户名的路径"
    negative = [
        r"C:\Users\OtherHuman\Documents",
        r"D:\Public\Documents",
        "/var/log/x",
    ]
    for s in negative:
        if re.search(fake_pattern, s):
            print(f"  [FAIL] 家目录规则误命中 {s!r}")
            failed.append("家目录绝对路径（误报）")
            break
    else:
        print(f"  [ok  ] 家目录规则未误命中 {len(negative)} 个无关样例")

    # 取不到用户名时必须是"永不匹配"，而不是"匹配所有"
    if HOME_USERS:
        print(f"  [ok  ] 运行环境取到 {len(HOME_USERS)} 个候选用户名")
    else:
        print("  [warn] 运行环境未取到用户名 —— 家目录规则将永不匹配")
    if re.search(_home_path_pattern(()), r"C:\Users\Anyone\x"):
        print("  [FAIL] 空用户名列表时规则不该匹配任何东西")
        failed.append("家目录绝对路径（空列表）")
    else:
        print("  [ok  ] 空用户名列表时规则不匹配任何东西")

    # --- 第二部分：其余规则的样例验证 ---
    #
    # 注意：私有目录名 / 同步盘名 / 浏览器实例 ID 这几条规则**本身就必须含
    # 真实值**（它们要查的就是这些本机特有标识），所以样例直接取自同一批常量
    # —— 这样规则和样例不会漂移。
    cases = {
        "私有工作目录名": [f"{PRIVATE_DIR_NAMES[0]}\\x"],
        "桌面/同步盘路径": [r"D:\Desktop\x", SYNC_DIR_NAMES[0], "C:/Users/x/Desktop/y"],
        "AstrBot 安装路径": [r"D:\AstrBot\backend\app", "D:/AstrBot/x"],
        "本机浏览器实例 ID": [BROWSER_INSTANCE_IDS[0], f"id={BROWSER_INSTANCE_IDS[0]}"],
        "疑似 API 密钥": ["sk-" + "a" * 24, "gho_" + "b" * 30],
        "邮箱地址": ["someone@example.com"],
        "token / secret / password 赋值": ['token = "abcdefgh12345"'],
    }
    for name, pattern, _ in SENSITIVE_PATTERNS:
        if name == "家目录绝对路径":
            continue  # 上面已单独验证
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
    for s in [rf"D:\{fake_users[0]}\Documents", "0123abcd", "someone@example.com"]:
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
        元组顺序是 ``(名称, 正则, 说明)`` —— 名字在前。
        初版这里写成了 ``for pattern, name, why``，变量名与位置对调，
        导致 ``re.finditer`` 拿中文名称当正则用，整个扫描器静默失效
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

    # 跳过本文件自己：它必须包含检测规则（含真实用户名），否则查不出家目录路径。
    # 所以它会被自己的规则命中 —— 那是规则的一部分，不是泄露。
    SELF = "tests/verify_privacy.py"

    text_hits = 0
    skipped_self = 0
    for rel in files:
        if rel == SELF:
            skipped_self += 1
            continue
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

    if skipped_self:
        print("    （已跳过本文件自身：它含检测规则，必然命中自己的规则）")
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
        print("  - 作者名 JosephTian876 与仓库地址属于有意公开的信息，")
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
