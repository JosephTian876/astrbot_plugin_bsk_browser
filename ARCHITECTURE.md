# astrbot_plugin_bsk_browser — 架构与实施方案

> 版本：v0.1.0 设计稿
> 目标读者：实现该插件的工程师（含 subagent）
> 依据：`D:\UwU\Documents\dshworkdir\astrbot-browserskill\research\` 下的 6 份调研报告

---

## 1. 项目目标

把本机 `bsk` CLI（腾讯 BrowserSkill）包装成 AstrBot 的 LLM 可调用工具，让机器人能操作用户**已登录的真实浏览器**。

**核心价值**：AstrBot 现有浏览器插件都是"开一个干净的无头浏览器"，用不了用户已登录的账号。本插件复用用户的真实登录态。

**非目标**（明确不做，避免范围蔓延）：
- 不实现 `evaluate`（执行任意 JS）—— 安全风险，v1 不开放
- 不捆绑 `bsk` 二进制 —— 合规红线，必须用户自装
- 不支持远程（非 loopback）模式 —— bsk 远程模式本身不支持文件传输

---

## 2. 硬约束（全部来自 AstrBot 4.28.1 源码实证，不可违反）

| # | 约束 | 源码证据 | 违反后果 |
|---|---|---|---|
| **C1** | 插件入口必须是 `main.py`（或与目录同名的 `.py`） | `star_manager._get_modules` L301-304 | 插件根本不被发现 |
| **C2** | `@filter.llm_tool` 装饰的函数**必须定义在 `main.py`** | `star_manager.py` L1281-1288：仅当 `ft.handler.__module__ == metadata.module_path` 才用 `functools.partial(raw_handler, metadata.star_cls)` 绑定 `self` | 工具定义在子模块 → **拿不到 self，调用时静默失败/TypeError** |
| **C3** | **绝不定义 `__del__`** | `star_manager.py` L1944-1963 是 `if "__del__" in ...: ... elif "terminate" in ...` | 定义了 `__del__` → `terminate()` **永不执行** → 子进程/会话泄漏 |
| **C4** | `terminate` / `initialize` 必须定义在**插件类自己的 `__dict__`** 里 | 同上，用 `cls.__dict__` 判断 | 继承来的不算，钩子不触发 |
| **C5** | 配置 schema 的 `type` 必须在 `DEFAULT_VALUE_MAP` 白名单内 | `int/float/bool/string/text/list/file/object/template_list/dict` | 非法 type → `TypeError` 插件**加载失败** |
| **C6** | `@filter.llm_tool` 的 docstring `Args:` 段是**参数 schema 的唯一来源**（不读函数类型注解） | `star_handler.py` L633-665 | 漏写 `Args:` → schema 为空且**静默失效**；类型名非法 → `ValueError` 加载失败 |
| **C7** | `Star.__init__(self, context, config=None)`，config 必须有默认值 | 框架用关键字参数调用 | 无默认值 → 无配置时实例化失败 |
| **C8** | 插件是**单例**，消息处理是**并发 task** | `metadata.star_cls` 实例化一次；`event_bus.py` `asyncio.create_task` | 多用户并发进入同一实例 → 必须按 umo 隔离状态 + 加锁 |
| **C9** | AstrBot 自带 Python **3.12**；Windows 下 `sys.stdout.encoding = gbk` | 实机实测 | 不显式指定 UTF-8 → `UnicodeDecodeError`（在 reader 线程抛，主线程只看到 `None`） |

---

## 3. 分层架构

**设计原则：框架耦合只允许出现在 `main.py` 一个文件里。`bsk/` 包是纯 Python，不 import astrbot，可脱离框架单测。**

```
┌──────────────────────────────────────────────────────────┐
│  main.py   ← 唯一的框架耦合层（必须叫 main.py，见 C1）      │
│  · BskBrowserPlugin(Star)  插件类、配置读取、生命周期钩子   │
│  · @filter.llm_tool 薄适配函数（必须在此，见 C2）           │
│    职责仅：参数解包 → 调用 bsk/ 服务 → 转成框架返回值        │
└───────────────────────┬──────────────────────────────────┘
                        │ 只依赖 bsk/ 包的公开 API
┌───────────────────────▼──────────────────────────────────┐
│  bsk/   ← 纯逻辑层（零 astrbot 依赖，可单测）               │
│  · models.py    数据模型（Session、Page、Shot、Result）     │
│  · errors.py    bsk 退出码/错误码 → 分类异常 → 中文提示      │
│  · config.py    原始 dict → 强类型 Settings（含校验与兜底）  │
│  · runner.py    子进程调用：超时、编码、退出码、JSON 容错    │
│  · session.py   会话生命周期：umo→session 映射、锁、显式 stop│
│  · pages.py     VOM 语义解析：标题提取、ref 提取、截断       │
│  · shots.py     截图：唯一路径、魔数校验、清理              │
│  · service.py   业务编排（tools 的纯逻辑实现，不含装饰器）   │
└──────────────────────────────────────────────────────────┘
```

**为什么这样分**：
- `C2` 强制工具函数必须在 `main.py`。若把业务逻辑也写进去，`main.py` 会变成 1000+ 行巨石（当前骨架就是 44KB 单文件）。
- 因此让 `main.py` 只做**薄适配**：每个工具函数 5-15 行，负责"取参数、调 service、包结果"。
- 真正的逻辑在 `bsk/service.py` 及其依赖，**不 import astrbot**，所以可以直接 `pytest` 跑，不需要启动 AstrBot。

---

## 4. 模块职责与接口契约

### 4.1 `bsk/errors.py`

```python
class BskError(Exception):
    """bsk 调用失败的基类。"""
    code: str          # bsk 的 error code，如 "not_found"
    exit_code: int     # 进程退出码
    friendly: str      # 给用户看的中文提示

class BskNotInstalled(BskError): ...   # 找不到 bsk 可执行文件
class BskTimeout(BskError): ...        # 超时
class BskSessionGone(BskError): ...    # code == "not_found" → 可重建会话
class BskSessionBusy(BskError): ...    # code == "session_busy" → 可短暂重试
class BskOutcomeUnknown(BskError): ... # 动作结果未知 → 禁止重试
class BskProtocolError(BskError): ...  # 输出不是 JSON（如 clap 参数错误）
```

**退出码映射**（实测 6 档）：`0` 成功 / `1` 用户错误 / `2` 协议传输 / `3` 浏览器CDP / `4` 超时 / `5` 版本不匹配。

### 4.2 `bsk/runner.py`

唯一与子进程打交道的地方。

```python
class BskRunner:
    def __init__(self, bsk_path: str, default_timeout: float): ...
    async def run(self, args: list[str], timeout: float | None = None) -> BskResult: ...
```

**必须处理的坑**（全部实测确认）：
1. `asyncio.create_subprocess_exec(*args)` —— 列表参数，**不过 shell**（无注入面）
2. **超时必须包住 `communicate()`** —— Windows 下 daemon 继承管道句柄可能导致永不返回
3. 超时后：先关 stdin 给 15 秒宽限（Windows 取消语义），再 `kill()`
4. stdout/stderr **显式 `.decode("utf-8", errors="replace")`** —— 见 C9
5. **错误 JSON 走 stdout 不是 stderr**；clap 参数错误是 stderr 纯文本 → 解析必须容错
6. 退出码非 0 即失败，**不能靠解析 JSON 判成败**

### 4.3 `bsk/session.py`

```python
class SessionManager:
    async def acquire(self, key: str) -> BskSession: ...   # 取或建
    async def release(self, key: str) -> None: ...         # 显式 stop
    async def release_all(self) -> None: ...               # terminate 用
```

**必须遵守**：
- `session stop <ID>` 的 id 是**位置参数**（唯一例外，其余命令用 `--session`）
- 每个 key 一把 `asyncio.Lock`，**禁止同 session 并发**（bsk 同时只允许 1 个 in-flight，否则 `session_busy`）
- **绝不使用 `session stop --all`** —— 会停掉别的程序（如 DSH）创建的会话
- 会话失效靠"用失败触发重建"（`not_found` → 重建 → 重试 1 次），**不要**每次操作前先探测
- `terminate` 里必须 `try/finally` 全部 stop，否则留下 Agent Window 打扰用户
- `session_busy` 等 100ms 重试一次；`outcome_unknown` **禁止重试**并标记会话为不确定态

### 4.4 `bsk/pages.py`

`observe` 返回的是**带标记的缩进文本树**，不是 JSON 结构：

```
@vom 1
@view 910x604
@layers 1 focus=L1
L1 page
  RootWebArea "Example Domain"
    paragraph "……"
    @e1 link "Learn more" [→ iana.org]
```

必须解析出：页面标题（`RootWebArea "..."`）、可交互元素（`@eN role "name"`）、ref 总数、是否截断。

**注意**：`observe` **没有**独立 `title`/`url` 字段，只能正则提取。
**注意**：`snapshot` 与 `observe` 实测输出逐字节相同且都不带截图 → **只调 `observe`**。

### 4.5 `bsk/shots.py`

- 必须**自己指定绝对路径**并保证唯一（含 session_id + 时间戳），否则并发互相覆盖
- `--out` 会**覆盖**已有文件
- 拿到路径后**三重校验**：文件存在、大小 == `byte_size`、**前 8 字节魔数**（某些 Chromium 构建请求 png 却返回 JPEG）
- 定期清理，避免磁盘无限增长
- 给 LLM 前考虑降采样（1850×1208 的原图很贵）

### 4.6 `bsk/config.py`

把 AstrBot 传来的原始 dict 转成强类型 `Settings`，**对每一项做类型校验和兜底**（配置可能被用户填成任何东西）。

---

## 5. 关键设计决策

### D1：注册路径选 `@filter.llm_tool`（而非 `context.add_llm_tools`）

- 前者由框架解析 docstring 生成 schema，**单一事实来源**，不易写错；
- 后者需手写 JSON schema，且要自己管 `handler_module_path`。
- 代价：工具函数必须在 `main.py`（C2），已由分层设计消化。

### D2：权限默认仅管理员，且可调（用户要求 #5）

- 插件自己实现 `admin_only`（默认 `true`），读插件配置，**可在 AstrBot 插件设置页直接改**；
- 每个工具函数入口统一调用一个 `_guard(event) -> str | None`；
- 额外支持 `allowed_users`（用户 ID 白名单），便于"只给某个人用"；
- 同时**兼容** AstrBot 原生的 `tool_permissions`（WebUI → 扩展组件）机制，两者取严。

### D3：不做 `evaluate`

`evaluate` 能让模型在用户已登录页面执行任意 JS，风险高于收益。v1 明确不实现，并在 README 说明。留 `# TODO` 但不暴露给模型。

### D4：会话键策略

`umo`（每个聊天会话一条）为默认，可选 `user`（每人一条）。键提取做三级兜底：`unified_msg_origin` → `session_id` → `sender_id`。

### D5：错误一律转成"模型能看懂的中文"

工具返回给 LLM 的字符串必须包含**下一步该怎么做**（例如"会话已过期，已自动重建，请重试"），而不是抛裸异常。

### D6：超时采用「取较大值」的单一规则

**规则一句话**：`command_timeout_sec` 是所有 bsk 命令的超时；每个命令的内置值是**下限**，
最终值 = `max(内置下限, command_timeout_sec)`。

- 内置下限 = "这个命令至少需要多久"。例如 `navigate` 必须 **大于** bsk 自身的 `--timeout`
  30s，否则我们会先把它掐掉、而它正要成功返回。
- 用户配置 = "我愿意等多久"。
- 取较大值，两者都不被违背。

| 命令 | 内置下限 |
|---|---|
| status / browsers / session list / console / network | 5s |
| observe | 15s |
| session start | 30s |
| navigate | 45s（必须 > bsk 自身默认 30s） |
| screenshot 视口 | 30s |
| screenshot --full-page | 180s |

**为什么不做更复杂的规则**（例如"快命令取 min、慢命令取 max"）：本插件的使用者是
编程新手，配置项的行为必须能用一句话说清。复杂规则会带来解释成本和"为什么调了没用"
的困惑 —— 而这正是改进前的老问题。

**AstrBot 侧的上限**：AstrBot 的 `tool_call_timeout` 默认 **120 秒**
（源码 `core/agent/run_context.py:19` 与 `core/config/agent_runner.py:33`），
超时后框架会抛 `tool <name> execution timeout`。
**但它是可调的**（`agent_runner.config.misc.tool_call_timeout`），
所以全页截图（内置下限 180s）并非不可能，只是需要用户**两处一起调大**：
本插件的 `command_timeout_sec` 与 AstrBot 的 `tool_call_timeout`。
这一点必须写进 README 的已知限制里，否则用户会以为插件有 bug。

---

## 6. 测试策略

**分四层 + 专项验证**。前两层不需要任何外部依赖，后两层需要真实环境。

| 层 | 范围 | AstrBot | 浏览器 | 脚本 |
|---|---|---|---|---|
| L1 单元测试 | `bsk/*` 纯逻辑：错误映射、VOM 解析、配置校验、截图魔数、会话状态机 | ❌ | ❌ | `tests/test_*.py`（468 个用例） |
| L2 契约测试 | `main.py` 能被真实 AstrBot import、6 个工具注册成功、docstring schema 正确、硬约束（无 `__del__` 等）满足 | ✅ | ❌ | `verify_astrbot_contract.py` |
| L3 服务层集成 | 真实调用 bsk：开→导航→读→截图→关，含并发与**会话过期自动重建** | ✅ | ✅ | `verify_integration.py` |
| L4 工具层端到端 | **直接 await `main.py` 里的 6 个工具函数**，验证权限门、参数校验、异步生成器行为、异常包装 | ✅ | ✅ | `verify_tools_e2e.py` |

**为什么必须有 L4**：L2 只证明工具"注册成功"，L3 只走到服务层。工具函数内部那层
（URL 校验、权限判定、`bsk_screenshot` 的 async generator、异常是否被吞掉）
只有 L4 能覆盖 —— 而那正是最容易出 bug、且出错时用户直接看到堆栈的地方。

**专项验证**（各自针对一类"单元测试覆盖不到"的风险）：

| 脚本 | 针对的风险 | 需要浏览器 |
|---|---|---|
| `verify_tool_execution_chain.py` | 走 **AstrBot 真实工具执行器**（`call_local_llm_tool` + partial 绑定 + async generator 消费），而非直接调函数 | ✅ |
| `verify_restart_real.py` | **跨进程**验证"强杀后重启自愈"，含他人会话干扰项 | ✅ |
| `verify_recover_real.py` | journal 恢复逻辑对**真实 daemon** 的行为（含碰撞防护） | ✅ |
| `verify_journal_safety.py` | 碰撞防护：id 相同但窗口号不同时**一条 stop 都不发** | ❌ |
| `verify_browser_ambiguity.py` | 多浏览器歧义：1 个免配置 / ≥2 报错 / 已配置尊重配置 | ❌ |
| `verify_config_pipeline.py` | 配置从文件 → `AstrBotConfig` → `Settings` → **实际 bsk 命令行参数**的完整贯通 | ❌ |
| `verify_config_type.py` / `verify_config_consistency.py` | 配置来源形态（dict vs `AstrBotConfig` 对象）、schema 与代码默认值一致 | ❌ |
| `verify_install.py` | 从**已提交文件**导出干净副本并加载，验证"别人拿到仓库能用" | ❌ |
| `verify_discovery.py` | AstrBot **自己的插件发现函数**能否找到本插件 | ❌ |
| `verify_failure_ux.py` | 环境未就绪时的提示质量（不能是 Python 堆栈） | ❌ |
| `verify_stop_timing_real.py` | `session stop` 真实耗时（为超时预算提供数据依据） | ✅ |
| `verify_wait_navigation_real.py` | 新增暴露的 `wait_for_navigation` 动作真实可用且只读 | ✅ |
| `verify_release_ready.py` | 发布前自检（34 项）：元数据、合规红线、架构约束、工作区卫生 | ❌ |

**运行方式**（用 AstrBot 自带解释器，因为插件就跑在它上面）：

```powershell
$py = "D:\AstrBot\backend\python\python.exe"
cd D:\UwU\Documents\dshworkdir\astrbot_plugin_bsk_browser
& $py -m unittest discover -s tests        # L1（468 个）
& $py tests\verify_astrbot_contract.py     # L2
& $py tests\verify_integration.py          # L3（需要浏览器）
& $py tests\verify_tools_e2e.py            # L4（需要浏览器）
& $py tests\verify_release_ready.py        # 发布前自检
```

**注意**：本机 `pytest` 不可用（`ModuleNotFoundError`），全部测试用 `unittest`。

**L3/L4 的强制安全边界**（这些脚本会真的操作浏览器）：
只访问 `example.com`；**绝不**借用用户标签页；不做 click/fill/press/upload/download/evaluate；
**绝不**使用 `session stop --all`（会误停用户的 DSH 会话）；结束时按精确 id 清理自己的会话。
断言"自己的会话没了"时，**只比对自己创建的 session id**，不能断言"浏览器会话数为 0"
（那会把别人的会话算进来而误报）。并且**以 daemon 为事实来源**：清理后再查一次
`session list`，必要时按精确 id 补刀，不要只信管理器自述"已清空"。

**L2/L4 的路径前提**：AstrBot 用 `__import__("data.plugins.<目录>.main")` 加载插件，
所以脚本需要把 `~/.astrbot` 放进 `sys.path`，且插件要真的安装在
`~/.astrbot/data/plugins/astrbot_plugin_bsk_browser/` 下。脚本已自行处理路径。

**★ 所有 import astrbot 的测试脚本必须先 `os.environ.setdefault("ASTRBOT_ROOT", ...)`**：
AstrBot 解析数据路径时优先读该变量，否则普通模式下用**当前工作目录**
（`astrbot_path.py:29-35`），会在项目里生成 `data/cmd_config.json`
（AstrBot 主配置，含 provider API 密钥与管理员 QQ 号）—— 而本仓库是要公开发布的。
这个坑**栽过 3 次**，现已由 `verify_release_ready.py` 静态扫描（AST 解析真实 import
语句）自动拦截。

---

## 6.1 已修复的真实 bug（回归测试守护，勿回退）

记录这些是因为它们都属于"只有真实环境才暴露"的类型，改动相关代码时容易重新引入。

| # | 缺陷 | 根因 | 表现 | 守护测试 |
|---|---|---|---|---|
| 1 | **stdin 自杀式取消** | `communicate()` 会在读取前关掉 stdin，而环境变量设了 `BSK_CANCEL_ON_STDIN_CLOSE=1`，bsk 把"stdin 被关"当成用户按 Ctrl-C | 随机的 `tool dispatch cancelled after extension cleanup`；并发 8 个 observe 只有 5 个成功 | `test_runner.py` + L3 的并发用例 |
| 2 | **GBK 编码崩溃** | Windows 下 Python 默认用 cp936 解码，页面含阿拉伯文/俄文时抛 `UnicodeDecodeError`，且异常在 reader 线程抛出，主线程只看到 `None` | 读取任何多语言页面即崩，且报错信息毫无指向性 | `test_runner.py::TestEncoding` |
| 3 | **VOM ref 前缀丢失** | 正则捕获组漏了 `e`，`@e1` 被存成 `"1"` | 传给 bsk `--ref` 的值非法，所有元素操作失效 | `test_pages.py` |
| 4 | **截图清理失效** | `cleanup_shots` 只下探一层，而文件写在 `shots/<session>/` 两层 | 清理永远返回 0，磁盘无限增长 | `test_shots.py` |
| 5 | **配置项形同虚设** | 各命令硬编码超时，忽略用户的 `command_timeout_sec` | 用户调大超时对慢页面毫无帮助 | `test_service.py` |
| 6 | **权限提示与实现相反** | `validate_settings` 的文案说白名单"不生效"，实际是白名单优先 | 用户按提示操作得到相反结果 | `test_config.py` |
| 7 | **多浏览器时静默随机选** | `probe_browser` 只在恰好 1 个时自动选，多个时返回空 → 交 bsk 自选；且 README 已承诺"会报错"但代码没做 | 用户连了 2 个浏览器时"有时候对有时候不对"，无从排查 | `verify_browser_ambiguity.py` |
| 8 | **测试自身泄漏会话** | 某用例把假 id `"zzzz"` 写进 args builder，导致懒创建的真实会话在重建时被遗弃 | 全量回归后 daemon 残留会话 | `verify_integration.py` 的差集断言 + 兜底强清 |
| 9 | **README 与实现方向相反** | `session_scope:"user"` 说"跨群共用"，实际键含 umo → 跨群独立 | 用户按文档理解会误判资源占用 | README 核对报告 |

### 6.2 已确认的**固有行为**（不是 bug，但必须如实告知）

这些是 bsk / 浏览器的固有性质，改不掉，只能让用户和模型都知道：

| 行为 | 实测证据 | 应对 |
|---|---|---|
| **会话回收重建后，页面状态丢失** | 新会话停在空白页（`RootWebArea` 无标题、`text` 仅 61 字符、`ref_count=0`） | README 如实描述；`service.observe()` 检测到重建时在返回文本前插入提示，避免模型把空白页当成"网页没内容" |
| **`session stop` 偶发瞬时失败** | bsk 0.3.2 会返回 `RpcError{ProtocolError,"Background execution cleanup timed out"}` | 已加有界重试（3 次）；journal + `recover_orphans()` 兜底 |
| `label` 常为空字符串 | `bsk browsers --json` 的 `label: ""` | 只用 `instance_id` 指定浏览器；展示时回退 `browser_name` |
| `snapshot` 与 `observe` 输出等价 | 实测逐字节相同且都不带截图 | 只用 `observe`，避免多花一倍时间 |
| `observe` 无独立 title/url 字段 | 只能从 `RootWebArea "..."` 正则提取 | `bsk/pages.py` 负责解析 |
| observe 视口 ≠ 截图像素 | 910x604 vs 1850x1208（DPR≈2） | 不要用 observe 坐标点截图位置 |

---

## 7. 版本控制

- 仓库根 = `D:\UwU\Documents\dshworkdir\astrbot_plugin_bsk_browser\`（插件目录本身即仓库根，便于直接 clone 进 `data/plugins/`）
- 分支模型：`main` 为稳定分支，功能在 `feat/*` 分支开发
- 提交规范：Conventional Commits（`feat:` / `fix:` / `test:` / `docs:` / `chore:`）
- 每个可工作状态打 tag（`v0.1.0` 等）
- **必须提交的文件**：`LICENSE`、`README.md`、`CHANGELOG.md`、`.gitignore`
- `.gitignore` 需排除：`__pycache__/`、`*.pyc`、测试产物、截图、`.venv/`

**协议**：`MIT OR AGPL-3.0-or-later` 双许可（见调研报告 `07-agpl-redteam.md` 的结论）。
**商标纪律**：插件名用 `astrbot_plugin_bsk_browser`，**不得**使用 "BrowserSkill" 作为产品名，README 必须含非官方声明。

---

## 8. 合规清单（发布前逐条自检）

- [ ] 不包含 `bsk` 二进制
- [ ] 不复制 Tencent/BrowserSkill 的任何源码/schema/skill 文档
- [ ] 不复制 AstrBot 源码（写了 AGPL 代码就必须 AGPL）
- [ ] 未使用官方模板 "Use this template" 建仓
- [ ] `LICENSE` 存在且为双许可
- [ ] README 含：非官方声明、致谢 Tencent/BrowserSkill、隐私提醒、已知限制
- [ ] 仓库公开（满足 AGPL §13）

---

## 9. 交付物

```
astrbot_plugin_bsk_browser/
├── main.py                  # 框架适配层（唯一 import astrbot 的地方）
├── bsk/                     # 纯逻辑包
│   ├── __init__.py
│   ├── models.py
│   ├── errors.py
│   ├── config.py
│   ├── runner.py
│   ├── session.py
│   ├── pages.py
│   ├── shots.py
│   └── service.py
├── tests/                   # L1 单元测试
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt         # 空（纯标准库）
├── LICENSE
├── README.md
├── CHANGELOG.md
├── ARCHITECTURE.md          # 本文件
└── .gitignore
```
