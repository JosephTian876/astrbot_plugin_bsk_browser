# 设计文档：astrbot_plugin_bsk_browser 页面交互容错与诊断弹性增强

## 1. 意图与背景 (Intent & Why)
线上机器人使用 `astrbot_plugin_bsk_browser` 操作网页时（如百度搜索结果翻页），频繁出现死循环报错：`页面交互失败：浏览器拒绝了这次操作：该动作只允许在 bsk 自己的窗口里执行。`。
经过追踪，发现该问题由三层原因级联引发：
1. **错误遮蔽**：`bsk/errors.py` 将所有 `code == "permission_denied"` 粗暴映射为“只允许在 bsk 窗口执行”，丢弃了真实的 `reason` 与底层 `detail`；
2. **视口外元素未滚动**：长页面底部元素（如 `@e27` 翻页按钮）超出视口范围，底层 `bsk` 抛出 `element_not_visible`，被错误提示误导为窗口权限问题；
3. **会话路由断裂**：`service.observe` 遗漏了 `_resolve(key)`，大模型传入 `session="owkd"` 时被当作新业务键处理，创建了位于 `about:blank` 的空幽灵会话。

本设计旨在修复上述缺陷，建立服务层自动滚动自愈机制，并透传真实底层错误细节，使大模型具备正确的环境感知与行动能力。

---

## 2. 目标、非目标与反目标 (Goals, Non-Goals, Anti-Goals)

### 目标 (Goals)
1. **精准的错误分类与诊断**：按 `reason` 区分 `permission_denied`，精准识别 `element_not_visible`、`frame_unobservable`、`agent_window_scope` 等，并在报错信息中完整回显 `reason` 和 `detail`；
2. **交互自愈（Auto-scroll on `element_not_visible`）**：在 `service.interact` 中感知 `element_not_visible`，自动调度 `scroll-to [TARGET]` 将元素滚入视口并单次重试交互；成功时显式在回执中说明已自动滚动；
3. **会话解析全局一致**：全面审计 `service.py`，确保所有接收 `key` 的公开接口（`observe`、`evaluate` 等）均经过 `_resolve(key)` 规范化，避免分裂幽灵会话；
4. **单元测试与回归防护**：编写自动化测试，覆盖错误分类、自愈重试、回执格式与会话解析。

### 非目标 (Non-Goals)
1. 不修改 `BrowserSkill`（Rust/TypeScript CLI 与浏览器扩展）本身的协议规范；
2. 不处理多视口复杂 iframe 嵌套中的跨域坐标穿透（由底层 `bsk` 协议负责）；
3. 不自动重试非幂等的非几何类错误（如网络断开、选择器语法错误、标签页关闭）。

### 反目标 (Anti-Goals)
1. **禁止无限重试或静默吞错**：自动滚动重试严格限制为最多 1 次；若重试后依然不可见，必须如实向大模型报错；
2. **禁止静默修改视口而不告知模型**：自动滚动必须在成功回执中明确提示，防止模型基于旧视口位置发生空间幻觉。

---

## 3. 约束条件 (Constraints)
1. 保持现有 7 个工具（`bsk_page`、`bsk_inspect`、`bsk_interact` 等）的 schema 与入参不变，完全向后兼容；
2. 保持与 AstrBot 插件运行环境兼容（Python 3.10+，使用标准库 `unittest` 进行测试验证）；
3. 所有改动集中在 `astrbot_plugin_bsk_browser` 代码仓库内。

---

## 4. 备选方案对比与决策 (Approaches)

### 方案 A：服务层内聚自愈与分层回显（已选定）
- **做法**：在 `service.interact` 内部统一处理 `element_not_visible` 拦截、`scroll-to` 构造、单次重试及 `auto_scrolled` 状态标记；在 `errors.py` 细化错误映射；在 `main.py` 渲染回执附注与完整错误信息。
- **优点**：在 `SessionManager` 会话锁生命周期内原子完成滚动与点击；所有通过 `interact` 的动作统一受益；表示层与服务层职责清晰。
- **代价**：需对 `errors.py`、`service.py`、`main.py` 协同修改。

### 方案 B：外层工具调度编排自愈
- **做法**：服务层不做重试，由 `main.py` 的 `_run_interact` 捕获异常后再发起 `scroll-to`。
- **缺点**：拆解成两次独立的外部交互，经历两次加锁与等待，增加并发竞态隐患，破坏模块封装性。

---

## 5. 详细实现设计 (Detailed Implementation)

### 5.1 错误分类与透传 (`bsk/errors.py` & `main.py`)
1. 在 `bsk/errors.py` 中更新 `from_bsk_result`：
   - 检查 `code == CODE_PERMISSION_DENIED`：
     - 若 `reason == "element_not_visible"`：
       `friendly="目标元素当前不在视口可视范围内，请滚动页面使其进入视野后再操作。"`
     - 若 `reason == "frame_unobservable"`：
       `friendly="当前页面处于空白页或受限协议页面（如 about:blank），无法读取或操作元素。"`
     - 若 `reason == "agent_window_scope"`：
       `friendly="浏览器拒绝了这次操作：该动作只允许在 bsk 自己的窗口里执行（若需操作其它标签页，请先使用 bsk_tabs(action='borrow') 借用）。"`
     - 若 `reason == "control_authority_required"`：
       `friendly="当前页面需要用户控制权或外部确认，暂时无法直接操作。"`
     - 默认兜底：
       `friendly=f"操作被浏览器权限策略拒绝（原因：{reason or '未知'}）。"`
   - 无论何种错误，确保 `BskError` 实例始终携带 `code`、`reason`、`detail`。
2. 在 `main.py` 全局异常捕获处（如行 932）：
   ```python
   except BskError as exc:
       extra_info = []
       if exc.reason:
           extra_info.append(f"原因: {exc.reason}")
       if exc.detail and exc.detail != exc.friendly:
           extra_info.append(f"详情: {exc.detail}")
       diag = f"（{'，'.join(extra_info)}）" if extra_info else ""
       return self._fail(f"{_ACTION_LABEL.get(tool, tool)}失败：{exc.friendly}{diag}")
   ```

### 5.2 服务层自动滚动重试 (`bsk/service.py`)
1. 在 `interact(self, key, action, *, tab_id=None, **opts)` 中：
   - 目标操作限定在有明确 `target` 的动作（`click`、`fill`、`hover` 等）；
   - 执行目标动作，捕获 `BskError`；
   - 若 `exc.reason == "element_not_visible"` 且入参中有非空 `target`：
     1. 记录日志：`[bsk.service] 目标 %s 不可见，尝试自动执行 scroll-to 后重试`；
     2. 构造 `scroll-to` 命令并执行：`argv_scroll, _ = self._build_interact_args("scroll-to", {"target": target})`；
     3. 再次执行原交互命令；
     4. 重试成功后，在返回字典中打标：`out["auto_scrolled"] = True`；
     5. 若重试仍抛出 `BskError`，则直接向上抛出，不再重试。

### 5.3 会话解析审查与补全 (`bsk/service.py`)
全面检查服务层所有接收 `key` 作为入参的方法：
- `observe(self, key, ...)`: 增加 `session_key = self._resolve(key)`，后续传给 `self.sessions.execute(session_key, ...)`；
- `evaluate(self, key, ...)`: 增加 `session_key = self._resolve(key)`；
- `open_page(self, key, ...)` / `screenshot(self, key, ...)` 等一并审查补全。

### 5.4 表示层显式通知 (`main.py`)
在 `_render_interact(action: str, result: dict) -> str` 中：
- 若 `result.get("auto_scrolled")` 为 True：
  在输出中追加一行：`lines.append("（注意：目标元素原先在视口外，插件已自动滚动页面至目标位置并完成操作。）")`。

---

## 6. 验证计划 (Verification Plan)
1. **单元测试** (`tests/test_errors.py` & `tests/test_service.py`)：
   - 测试各 `reason` 对应的 `BskError.friendly` 映射是否准确；
   - Mock 测试 `interact` 首次触发 `element_not_visible` 时自动触发 `scroll-to` 并成功重试；
   - Mock 测试二次重试仍失败时正确抛出异常；
   - 测试通过 `session_id` 调用 `observe` 时不会在 `SessionManager` 中新建条目；
2. **端到端实机验证**：
   - 运行项目现有测试集：`python tests/verify_failure_ux.py` 等；
   - 在远程环境或测试用例中验证长页面翻页（如百度第 2 页点击）自愈流程。
