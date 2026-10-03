"""L3 真实集成测试：用**本插件自己的代码**驱动真实浏览器。

和之前调研阶段的验证不同 —— 那时是直接敲 bsk 命令；这里走的是
``BskService`` → ``SessionManager`` → ``BskRunner`` 这条本插件的真实调用链，
验证的是**我们写的代码**能不能跑通，而不只是 bsk 能不能跑通。

安全边界（用户只授权访问无害页面）：
- 只访问 ``https://example.com`` 与本插件自己开出来的空白页；
- 不借用任何用户标签页；
- 只做 click/fill 之类的动作在 example.com 的链接上？**不** —— 本项目
  集成测试只做「读」相关的动作（navigate / observe / screenshot），
  不做任何会改变页面状态的操作，也不做 evaluate；
- 不使用 ``session stop --all``；
- 结束必须显式 stop 自己创建的会话。

用法：
    python tests/verify_integration.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))

from bsk.config import Settings, parse_settings  # noqa: E402
from bsk.errors import BskError  # noqa: E402
from bsk.service import BskService  # noqa: E402
from bsk.shots import sniff_format  # noqa: E402

TARGET_URL = "https://example.com"
TEST_KEY = "integration-test:local"

RESULTS: list[tuple[str, bool, str]] = []

# 本测试创建过的会话 id，用于精确判断"自己有没有泄漏"。
created_session_ids: set[str] = set()


async def _daemon_session_ids(service: BskService) -> set[str]:
    """直接问 daemon 现在有哪些会话 id。

    用 ``bsk session list --json`` 而不是 ``browsers`` 的 ``session_count`` ——
    后者会把**所有**会话算进来（包括用户的 DSH、别的聊天会话），
    无法区分哪些是本测试创建的。
    """
    result = await service.runner.run(["session", "list", "--json"], timeout=10)
    data = result.data or []
    if not isinstance(data, list):
        return set()
    return {str(s.get("session_id")) for s in data if isinstance(s, dict)}


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if detail else ""))


async def run() -> int:
    print("=" * 72)
    print("L3 真实集成测试：本插件代码驱动真实浏览器")
    print("=" * 72)

    # 允许用环境变量覆盖，便于在别的机器上跑。
    raw_config = {
        "bsk_path": os.environ.get("BSK_PATH", "bsk"),
        "command_timeout_sec": 60,
        "max_sessions": 2,
        "admin_only": True,
    }
    settings: Settings = parse_settings(raw_config)
    service = BskService(settings)

    session_id = ""
    try:
        # --- 1. 环境探测 ---
        try:
            browsers = await service.list_browsers()
            record(
                "列出已连接浏览器",
                bool(browsers),
                f"{len(browsers)} 个：" + ", ".join(b.instance_id for b in browsers)
                if browsers
                else "没有已连接的浏览器（扩展没装或没连上）",
            )
            if not browsers:
                print("\n没有可用浏览器，后续用例无法继续。")
                return 1
        except BskError as exc:
            record("列出已连接浏览器", False, exc.friendly)
            return 1

        # --- 2. 打开网页（走 service.open_page，它会 navigate + observe）---
        try:
            nav, observation = await service.open_page(TEST_KEY, TARGET_URL)
            session = await service.sessions.acquire(TEST_KEY)
            session_id = session.session_id
            created_session_ids.add(session_id)
            record(
                "打开网页",
                bool(session_id),
                f"session={session_id!r}, 落点={nav.final_url or nav.url}",
            )
            record(
                "读到页面标题",
                "Example Domain" in (observation.title or ""),
                f"title={observation.title!r}",
            )
            record(
                "解析出可交互元素",
                observation.ref_count >= 1 and len(observation.refs) >= 1,
                f"ref_count={observation.ref_count}, refs={[r.ref for r in observation.refs][:5]}",
            )
        except BskError as exc:
            record("打开网页", False, exc.friendly)
            traceback.print_exc()
            return 1

        # --- 3. 渲染给模型的摘要 ---
        try:
            page_text = service.render_page(observation)
            record(
                "渲染页面摘要",
                bool(page_text) and len(page_text) <= settings.max_page_chars + 200,
                f"{len(page_text)} 字符（上限 {settings.max_page_chars}）",
            )
        except Exception as exc:  # noqa: BLE001
            record("渲染页面摘要", False, repr(exc))

        # --- 4. 截图 ---
        try:
            payload = await service.screenshot(TEST_KEY, full_page=False)
            exists = Path(payload.path).is_file()
            fmt = sniff_format(payload.path) if exists else "unknown"
            record(
                "截图并校验",
                exists and fmt == "png" and payload.width > 0,
                f"{payload.width}x{payload.height}, {payload.byte_size}B, 格式={fmt}",
            )
            record(
                "截图路径唯一且非默认 TEMP",
                "bsk-screenshot-" not in payload.path,
                payload.path,
            )
        except BskError as exc:
            record("截图并校验", False, exc.friendly)

        # --- 5. 纯 Python 的截图去重（多次调用路径必须不同）---
        try:
            p1 = await service.screenshot(TEST_KEY, full_page=False)
            p2 = await service.screenshot(TEST_KEY, full_page=False)
            record("连续截图路径不覆盖", p1.path != p2.path, f"{Path(p1.path).name} vs {Path(p2.path).name}")
        except BskError as exc:
            record("连续截图路径不覆盖", False, exc.friendly)

        # --- 6. 会话复用（同 key 不该新建）---
        try:
            again = await service.sessions.acquire(TEST_KEY)
            record("会话复用", again.session_id == session_id, f"{again.session_id} vs {session_id}")
            stats = service.sessions.stats()
            started = (stats.get("counters") or {}).get("started", "?")
            record("未重复创建会话", started == 1, f"started={started}")
        except Exception as exc:  # noqa: BLE001
            record("会话复用", False, repr(exc))

        # --- 7. 并发安全：同时发多条 observe，不该触发 session_busy ---
        try:
            tasks = [service.observe(TEST_KEY) for _ in range(8)]
            outs = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [o for o in outs if isinstance(o, BaseException)]
            ok_count = len(outs) - len(errors)
            record(
                "并发 8 个 observe 不冲突",
                not errors,
                f"成功 {ok_count}/8" + (f"，错误：{errors[0]!r}" if errors else ""),
            )
            busy = (service.sessions.stats().get("counters") or {}).get("busy_retries", 0)
            print(f"       （busy_retries={busy}，为 0 说明锁完全避免了冲突）")
        except Exception as exc:  # noqa: BLE001
            record("并发 8 个 observe 不冲突", False, repr(exc))

        # --- 8. 控制台读取（验证 entries 缺失时不崩）---
        try:
            log = await service.read_console(TEST_KEY)
            text = service.render_console(log)
            record("读取控制台日志", isinstance(text, str), f"{len(log.entries)} 条")
        except BskError as exc:
            record("读取控制台日志", False, exc.friendly)

        # --- 9. 诊断信息 ---
        try:
            info = await service.status(TEST_KEY)
            rendered = service._render_status(info) if hasattr(service, "_render_status") else ""
            record(
                "诊断信息可用",
                bool(info.get("bsk_resolved")) and bool(info.get("browsers")),
                f"bsk={info.get('bsk_resolved')}",
            )
        except Exception as exc:  # noqa: BLE001
            record("诊断信息可用", False, repr(exc))

        # --- 10. 错误路径：不存在的会话应归类为可重建 ---
        try:
            from bsk.errors import BskSessionGone

            await service.sessions.execute(
                "never-created-key",
                lambda sid: ["observe", "--session", "zzzz", "--json"],
                timeout=5.0,
            )
            record("错误路径分类", False, "本应抛错却成功了")
        except BskSessionGone:
            record("错误路径分类", True, "not_found 正确归类为 BskSessionGone")
        except Exception as exc:  # noqa: BLE001
            record("错误路径分类", False, f"意外的异常类型：{type(exc).__name__}: {exc}")

    finally:
        # --- 清理：必须显式 stop，且只用精确 id ---
        print()
        print("-" * 72)
        try:
            closed = await service.shutdown()
            print(f"清理：关闭了 {closed} 个会话")

            # ★ 断言必须精确到"**本测试创建的那些**会话没了"。
            #   不能断言"浏览器上 session_count == 0"：那会把别人（用户的
            #   DSH、别的聊天会话、甚至上一次失败运行遗留的会话）也算进来，
            #   从而误报。这里用 daemon 的实际会话清单做差集比对。
            remaining_ids = await _daemon_session_ids(service)
            leaked = remaining_ids & created_session_ids
            record(
                "清理后本测试的会话无残留",
                not leaked,
                f"泄漏 {sorted(leaked)}" if leaked else f"已全部关闭（daemon 里还有 {len(remaining_ids)} 个不属于本测试的会话）",
            )
        except Exception as exc:  # noqa: BLE001
            record("清理后本测试的会话无残留", False, repr(exc))

    print()
    print("=" * 72)
    failed = [r for r in RESULTS if not r[1]]
    print(f"用例：{len(RESULTS)}，失败：{len(failed)}")
    for name, _, detail in failed:
        print(f"  [FAIL] {name}: {detail}")
    print("结果：" + ("全部通过" if not failed else "有失败"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
