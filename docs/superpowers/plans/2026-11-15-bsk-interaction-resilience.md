# astrbot_plugin_bsk_browser 页面交互容错与诊断弹性增强实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `astrbot_plugin_bsk_browser` 中因错误掩蔽、视口外元素未滚动及会话路由断裂导致的死循环报错，建立自动滚动自愈机制与透明化诊断。

**Architecture:** 在 `bsk/errors.py` 解耦 `permission_denied` 为细粒度错误并在 `main.py` 透传真实详情；在 `bsk/service.py` 补全 `_resolve(key)` 防止幽灵会话；在 `service.interact` 中对 `element_not_visible` 自动调度 `scroll-to` 并重试一次，在 `main.py` 显式呈现“自动滚动”附注。

**Tech Stack:** Python 3.10+, `unittest`, AstrBot Plugin Framework, BrowserSkill CLI (`bsk`)

**Spec:** [docs/superpowers/specs/2026-11-15-bsk-interaction-resilience-design.md](file:///run/media/smileyik/G/Workspace/ai/bsk/astrbot_plugin_bsk_browser/docs/superpowers/specs/2026-11-15-bsk-interaction-resilience-design.md)

## Global Constraints

- 保持 7 个操作浏览器的工具 schema 与入参完全向后兼容；
- 自动滚动重试严格限制为最多 1 次，严禁无限重试或静默吞错；
- 自动滚动生效后必须在成功回执文本中显式告知大模型，防止空间认知偏差；
- 报错信息必须同时包含人话指导（friendly）与底层诊断证据（reason、detail）；
- 使用 Python 标准库 `unittest` 编写测试，测试执行零网络依赖。

## Review Focus

- **无 target 动作遇到不可见错误**：例如 `press` 动作不携带 target，不能触发 `scroll-to`，应原样向上抛出；
- **滚动后仍不可见**：自动重试后若依然返回 `element_not_visible`，必须终止重试并抛出带详细原因的错误，不能死循环；
- **外来 session_id 解析**：传入未注册的外来 session_id 时，必须被 `_resolve` 正确拦截为 `foreign_session`，不访问 daemon；
- **只读动作不重试**：`observe`、`snapshot` 等只读检查动作不触发 `scroll-to` 交互重试；
- **超长错误详情裁剪**：当底层 `detail` 是超长堆栈或复杂 JSON 时，回显格式需做长度限制（<=120字符），防止污染大模型上下文。

---

### Task 1: 错误分类解耦与诊断信息结构化 (`bsk/errors.py`)

**Files:**
- Modify: `astrbot_plugin_bsk_browser/bsk/errors.py:318-326`
- Test: `astrbot_plugin_bsk_browser/tests/test_errors.py`

**Interfaces:**
- Consumes: `BskResult(code, message, data, exit_code)`
- Produces: `BskError.friendly`, `BskError.reason`, `BskError.detail`, `BskError.format_diagnostic() -> str`

- [x] **Step 1: 编写错误细分的失败单元测试**

在 `astrbot_plugin_bsk_browser/tests/test_errors.py` 中添加测试类 `TestPermissionDeniedReasons`：
```python
class TestPermissionDeniedReasons(unittest.TestCase):
    def test_element_not_visible_friendly(self) -> None:
        err = errors.from_bsk_result({
            "code": "permission_denied",
            "message": "element not visible",
            "data": {"reason": "element_not_visible"},
            "exit_code": 1,
        })
        self.assertIn("不在视口可视范围内", err.friendly)
        self.assertNotIn("只允许在 bsk 自己的窗口里执行", err.friendly)
        self.assertEqual(err.reason, "element_not_visible")

    def test_frame_unobservable_friendly(self) -> None:
        err = errors.from_bsk_result({
            "code": "permission_denied",
            "message": "unobservable frame",
            "data": {"reason": "frame_unobservable"},
            "exit_code": 1,
        })
        self.assertIn("空白页", err.friendly)
        self.assertEqual(err.reason, "frame_unobservable")

    def test_agent_window_scope_friendly(self) -> None:
        err = errors.from_bsk_result({
            "code": "permission_denied",
            "message": "window scope",
            "data": {"reason": "agent_window_scope"},
            "exit_code": 1,
        })
        self.assertIn("只允许在 bsk 自己的窗口里执行", err.friendly)
        self.assertEqual(err.reason, "agent_window_scope")

    def test_format_diagnostic(self) -> None:
        err = errors.from_bsk_result({
            "code": "permission_denied",
            "message": "bounds outside",
            "data": {"reason": "element_not_visible"},
            "exit_code": 1,
        })
        diag = err.format_diagnostic()
        self.assertIn("原因: element_not_visible", diag)
        self.assertIn("详情: bounds outside", diag)
```

- [x] **Step 2: 运行测试验证失败**

运行：`python -m unittest tests/test_errors.py`
预期：FAIL（`AssertionError: '不在视口可视范围内' not found in '浏览器拒绝了这次操作：该动作只允许在 bsk 自己的窗口里执行。'`）

- [x] **Step 3: 在 `bsk/errors.py` 中实现细化错误分类与 `format_diagnostic`**

在 `BskError` 类中增加方法：
```python
def format_diagnostic(self, max_detail_len: int = 120) -> str:
    """格式化底层诊断信息供回显。"""
    parts = []
    if self.reason:
        parts.append(f"原因: {self.reason}")
    det = (self.detail or "").strip()
    if det and det != self.friendly.strip():
        if len(det) > max_detail_len:
            det = det[: max_detail_len - 3] + "..."
        parts.append(f"详情: {det}")
    return f"（{'，'.join(parts)}）" if parts else ""
```

在 `from_bsk_result` 中替换单调的 `CODE_PERMISSION_DENIED` 分支：
```python
if code == CODE_PERMISSION_DENIED:
    if reason == "element_not_visible":
        friendly = "目标元素当前不在视口可视范围内，请滚动页面使其进入视野后再操作。"
    elif reason == "frame_unobservable":
        friendly = "当前页面处于空白页或受限协议页面（如 about:blank），无法读取或操作元素。"
    elif reason == "agent_window_scope":
        friendly = "浏览器拒绝了这次操作：该动作只允许在 bsk 自己的窗口里执行（若需操作其它标签页，请先使用 bsk_tabs(action='borrow') 借用）。"
    elif reason == "control_authority_required":
        friendly = "当前页面需要用户控制权或外部确认，暂时无法直接操作。"
    else:
        friendly = f"操作被浏览器权限策略拒绝（原因：{reason or '未知'}）。"

    return BskError(
        detail,
        friendly=friendly,
        code=code,
        exit_code=exit_code,
        reason=reason,
    )
```

- [x] **Step 4: 重新运行测试验证通过**

运行：`python -m unittest tests/test_errors.py`
预期：PASS（所有测试通过）

- [x] **Step 5: 提交更改**

```bash
git add bsk/errors.py tests/test_errors.py
git commit -m "fix(errors): decouple permission_denied by reason and add format_diagnostic"
```

---

### Task 2: 全局会话解析一致性补全 (`bsk/service.py`)

**Files:**
- Modify: `astrbot_plugin_bsk_browser/bsk/service.py:586-650, 832-850`
- Test: `astrbot_plugin_bsk_browser/tests/test_service.py`

**Interfaces:**
- Consumes: `self._resolve(key) -> str`
- Produces: `service.observe(key, ...)`, `service.evaluate(key, ...)` 正确绑定已知 session_key

- [x] **Step 1: 编写通过 session_id 调用 observe 不分裂新会话的失败测试**

在 `astrbot_plugin_bsk_browser/tests/test_service.py` 中添加测试：
```python
def test_observe_resolves_session_id_to_existing_key(self) -> None:
    # 模拟已知 key 为 "default:chat:1" 且持有 session_id 为 "owkd"
    service = self._create_service()
    service._owned_records = {"default:chat:1": "owkd"}
    # 当调用 observe(key="owkd") 时，应解析为 "default:chat:1" 并在对应 entry 执行
    # 而非向 SessionManager 传 "owkd" 导致新建条目
    resolved_key = service._resolve("owkd")
    self.assertEqual(resolved_key, "default:chat:1")
```

- [x] **Step 2: 运行测试验证**

运行：`python -m unittest tests/test_service.py`
预期：确认测试基准

- [x] **Step 3: 在 `bsk/service.py` 中补全 `observe` 与 `evaluate` 的 `_resolve` 调用**

修改 `observe`（行 586 附近）：
```python
async def observe(
    self,
    key: str,
    *,
    cursor: str = "",
    max_depth: int | None = None,
    max_tokens: int | None = None,
    tab_id: int | None = None,
) -> PageObservation:
    session_key = self._resolve(key)
    counters_before = self.sessions.stats().get("counters", {})
    # 后续 self.sessions.execute(session_key, ...)
```

修改 `evaluate`（行 832 附近）：
```python
async def evaluate(self, key: str, expression: str) -> EvaluateResult:
    session_key = self._resolve(key)
    # 后续 self.sessions.execute(session_key, ...)
```

并全面审计 `open_page`、`screenshot` 等其他接收 `key` 的入口，确保无裸 `key` 直接进入 `self.sessions.execute`。

- [x] **Step 4: 运行现有服务层测试验证回归**

运行：`python -m unittest tests/test_service.py`
预期：PASS（169+ 测试全部通过）

- [x] **Step 5: 提交更改**

```bash
git add bsk/service.py tests/test_service.py
git commit -m "fix(service): resolve session key before executing observe and evaluate"
```

---

### Task 3: 视口外元素点击自愈与重试 (`bsk/service.py`)

**Files:**
- Modify: `astrbot_plugin_bsk_browser/bsk/service.py:2440-2525`
- Test: `astrbot_plugin_bsk_browser/tests/test_service.py`

**Interfaces:**
- Consumes: `bsk scroll-to [TARGET]`, `BskError.reason == "element_not_visible"`
- Produces: `service.interact(...) -> dict` containing `"auto_scrolled": bool`

- [x] **Step 1: 编写针对 `interact` 在 `element_not_visible` 时自动滚动的单元测试**

在 `astrbot_plugin_bsk_browser/tests/test_service.py` 中增加用例：
```python
class TestInteractAutoScroll(unittest.IsolatedAsyncioTestCase):
    async def test_interact_auto_scrolls_and_retries_on_element_not_visible(self) -> None:
        # Mock SessionManager.execute：
        # 第 1 次 click 抛出 BskError(reason="element_not_visible")
        # 第 2 次为 scroll-to 成功
        # 第 3 次 click 成功
        ...
        result = await service.interact("my_key", "click", target="@e27")
        self.assertTrue(result.get("auto_scrolled"))

    async def test_interact_fails_if_retry_still_not_visible(self) -> None:
        # Mock SessionManager.execute：
        # 第 1 次 click 抛出 BskError(reason="element_not_visible")
        # 第 2 次 scroll-to 成功
        # 第 3 次 click 依然抛出 BskError(reason="element_not_visible")
        # 应直接向上抛出，不再尝试第 4 次
        ...
        with self.assertRaises(BskError) as ctx:
            await service.interact("my_key", "click", target="@e27")
        self.assertEqual(ctx.exception.reason, "element_not_visible")
```

- [x] **Step 2: 运行测试验证失败**

运行：`python -m unittest tests/test_service.py -k TestInteractAutoScroll`
预期：FAIL（`result.get('auto_scrolled')` 为 False 或未被拦截重试）

- [x] **Step 3: 在 `service.interact` 中实现自动滚动重试**

修改 `service.interact`：
```python
        session_key = self._resolve(key)
        argv, readonly = self._build_interact_args(action, opts)
        target = str(opts.get("target") or "").strip()
        auto_scrolled = False

        # 尝试执行交互
        try:
            result = await self.sessions.execute(
                session_key,
                lambda sid: self._with_tab_id(argv, tab_id) + ["--session", sid, "--json"],
                timeout=self._timeout(self._interact_timeout_builtin(action)),
                allow_uncertain=readonly,
            )
        except BskError as exc:
            # 只有有明确目标元素的动作，且错误是 element_not_visible 时才尝试 scroll-to
            if exc.reason == "element_not_visible" and target and action != "scroll-to":
                self._logger.info("目标 %s 不在视口内，尝试自动 scroll-to 后重试交互 %s", target, action)
                scroll_argv, scroll_readonly = self._build_interact_args("scroll-to", {"target": target})
                await self.sessions.execute(
                    session_key,
                    lambda sid: self._with_tab_id(scroll_argv, tab_id) + ["--session", sid, "--json"],
                    timeout=self._timeout(self._interact_timeout_builtin("scroll-to")),
                    allow_uncertain=scroll_readonly,
                )
                # 重新执行原交互
                result = await self.sessions.execute(
                    session_key,
                    lambda sid: self._with_tab_id(argv, tab_id) + ["--session", sid, "--json"],
                    timeout=self._timeout(self._interact_timeout_builtin(action)),
                    allow_uncertain=readonly,
                )
                auto_scrolled = True
            else:
                raise

        out: dict[str, Any] = {
            "session": self._session_field(session_key),
            "action": action,
            "tab_id": self._payload_tab_id(result.data),
            "auto_scrolled": auto_scrolled,
            ...
        }
```

- [x] **Step 4: 运行测试验证通过**

运行：`python -m unittest tests/test_service.py -k TestInteractAutoScroll`
预期：PASS

- [x] **Step 5: 提交更改**

```bash
git add bsk/service.py tests/test_service.py
git commit -m "feat(service): auto-scroll target element into view on element_not_visible"
```

---

### Task 4: 用户回执呈现与报错诊断格式化 (`main.py`)

**Files:**
- Modify: `astrbot_plugin_bsk_browser/main.py:930-936, 1564-1595`
- Test: `astrbot_plugin_bsk_browser/tests/test_tools.py`

**Interfaces:**
- Consumes: `result["auto_scrolled"]`, `exc.format_diagnostic()`
- Produces: 格式化中文返回文本

- [x] **Step 1: 编写展示层回执与报错格式化的失败单元测试**

在 `astrbot_plugin_bsk_browser/tests/test_tools.py` 中增加用例：
```python
def test_render_interact_includes_auto_scrolled_notice(self) -> None:
    res = MainPlugin._render_interact("click", {
        "session": "owkd",
        "target": "@e27",
        "auto_scrolled": True,
    })
    self.assertIn("（注意：目标元素原先位于视口外，已自动将其滚动至视口中心并完成操作。）", res)

def test_run_tool_exception_includes_diagnostic(self) -> None:
    # 模拟抛出带 reason 和 detail 的 BskError，检查返回文本包含诊断括号
    ...
```

- [x] **Step 2: 运行测试验证失败**

运行：`python -m unittest tests/test_tools.py`
预期：FAIL

- [x] **Step 3: 更新 `main.py` 的渲染与异常捕获**

在 `_render_interact` 中添加 `auto_scrolled` 回显逻辑：
```python
        target = result.get("target")
        if target:
            lines.append(f"目标元素：{target}")
        if result.get("auto_scrolled"):
            lines.append("（注意：目标元素原先位于视口外，已自动将其滚动至视口中心并完成操作。）")
        if result.get("changed"):
            lines.append(
                "这个动作会改变页面，接下来请用 bsk_inspect(action=\"observe\") "
                "确认结果（页面变化后元素编号会变，不要沿用旧的 @eN）。"
            )
```

在全局异常捕获（约行 932）及其他调用点的异常渲染中：
```python
        except BskError as exc:
            diag = exc.format_diagnostic()
            return self._fail(
                f"{_ACTION_LABEL.get(tool, tool)}失败：{exc.friendly}{diag}"
            )
```

- [x] **Step 4: 运行测试验证通过**

运行：`python -m unittest tests/test_tools.py`
预期：PASS

- [x] **Step 5: 提交更改**

```bash
git add main.py tests/test_tools.py
git commit -m "feat(main): expose auto_scrolled note and append diagnostics on error"
```

---

### Task 5: 全链路集成验证与回归测试

**Files:**
- Create: `astrbot_plugin_bsk_browser/tests/test_interaction_resilience.py`
- Test: 全量运行 `tests/` 目录下的自动化用例

- [x] **Step 1: 编写全链路集成测试文件 `test_interaction_resilience.py`**

包含测试用例：
1. `test_e2e_element_not_visible_auto_scroll_click_flow`：验证 `bsk_interact(click, @e27)` 从不可见到触发 `scroll-to` 再到成功回执的全链路渲染；
2. `test_e2e_observe_with_session_id_keeps_session`：验证通过 `session_id` 连续读取 observe 不会触发 `session rebuild` 提示；
3. `test_e2e_true_agent_window_scope_error`：验证真实的窗口越界能准确提示借用。

- [x] **Step 2: 运行新集成测试**

运行：`python -m unittest tests/test_interaction_resilience.py`
预期：PASS

- [x] **Step 3: 运行插件全量单元测试集**

运行：
```bash
python -m unittest discover tests/ "test_*.py"
```
预期：全部通过，0 失败，0 错误

- [x] **Step 4: 运行用户验收脚本**

运行：
```bash
python tests/verify_failure_ux.py
```
预期：PASS

- [x] **Step 5: 最终提交**

```bash
git add tests/test_interaction_resilience.py
git commit -m "test: add integration test suite for interaction resilience and diagnostics"
```
