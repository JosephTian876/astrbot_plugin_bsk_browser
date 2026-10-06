# astrbot_plugin_bsk_browser — 架构与实施方案

> 版本：v0.3.0（8 个主工具 + 8 个兼容旧工具；默认只注册 12 个）
> 目标读者：实现该插件的工程师（含 subagent）
> 依据：本仓库外的调研资料（6 份调研报告，未随仓库分发）+ 本机实测
>
> 全部数字（工具数、action 数、用例数、字符数）均为实测值，未估算。
> 唯一事实源是 `bsk/tools.py`（工具规格）与 `tests/`（测试），本文若与代码冲突以代码为准。
>
> **可交互架构图**：`docs/architecture.html`（用浏览器打开，支持明暗主题、缩放、
> 语义检索、关系追踪、导览视图）。源文件是 `docs/architecture.json`，
> 用 archify 重新渲染即可；图里声明了代码证据（`bsk/tools.py` 的 `TOOL_SCHEMAS`），
> 渲染时会校验仓库 revision 是否与图一致。

---

## 1. 项目目标

把本机 `bsk` CLI（腾讯 BrowserSkill）包装成 AstrBot 的 LLM 可调用工具，让机器人能操作用户已登录的真实浏览器。

**核心价值**：AstrBot 现有浏览器插件都是"开一个干净的无头浏览器"，用不了用户已登录的账号。本插件复用用户的真实登录态。

非目标（明确不做，避免范围蔓延）：
- 不**默认**开放 `evaluate`（执行任意 JS）—— 安全风险；v0.1.0 起它作为独立工具 `bsk_evaluate` 提供，但默认关闭且强制管理员（见 §5 D7）
- 不捆绑 `bsk` 二进制 —— 合规红线，必须用户自装
- 不支持远程（非 loopback）模式 —— bsk 远程模式本身不支持文件传输

---

## 2. 硬约束（全部来自 AstrBot 4.28.1 源码实证，不可违反）

| # | 约束 | 源码证据 | 违反后果 |
|---|---|---|---|
| **C1** | 插件入口必须是 `main.py`（或与目录同名的 `.py`） | `star_manager._get_modules` L301-304 | 插件根本不被发现 |
| **C2** | `@filter.llm_tool` 装饰的函数必须定义在 `main.py` | `star_manager.py` L1281-1288：仅当 `ft.handler.__module__ == metadata.module_path` 才用 `functools.partial(raw_handler, metadata.star_cls)` 绑定 `self` | 工具定义在子模块 → **拿不到 self，调用时静默失败/TypeError** |
| **C3** | 绝不定义 `__del__` | `star_manager.py` L1944-1963 是 `if "__del__" in ...: ... elif "terminate" in ...` | 定义了 `__del__` → `terminate()` **永不执行** → 子进程/会话泄漏 |
| **C4** | `terminate` / `initialize` 必须定义在插件类自己的 `__dict__` 里 | 同上，用 `cls.__dict__` 判断 | 继承来的不算，钩子不触发 |
| **C5** | 配置 schema 的 `type` 必须在 `DEFAULT_VALUE_MAP` 白名单内 | `int/float/bool/string/text/list/file/object/template_list/dict` | 非法 type → `TypeError` 插件**加载失败** |
| **C6** | `@filter.llm_tool` 的 docstring `Args:` 段是参数 schema 的唯一来源（不读函数类型注解） | `star_handler.py` L633-665 | 漏写 `Args:` → schema 为空且**静默失效**；类型名非法 → `ValueError` 加载失败 |
| **C7** | `Star.__init__(self, context, config=None)`，config 必须有默认值 | 框架用关键字参数调用 | 无默认值 → 无配置时实例化失败 |
| **C8** | 插件是单例，消息处理是并发 task | `metadata.star_cls` 实例化一次；`event_bus.py` `asyncio.create_task` | 多用户并发进入同一实例 → 必须按 umo 隔离状态 + 加锁 |
| **C9** | AstrBot 自带 Python 3.12；Windows 下 `sys.stdout.encoding = gbk` | 实机实测 | 不显式指定 UTF-8 → `UnicodeDecodeError`（在 reader 线程抛，主线程只看到 `None`） |

---

## 3. 分层架构

**设计原则：框架耦合只允许出现在 `main.py` 一个文件里。`bsk/` 包是纯 Python，既不 import astrbot，也不 import `logging`（内置日志模块），可脱离框架单测。**

日志由 `main.py` 从 `astrbot.api` 取来后**注入**（`BskService(..., logger=...)` → `SessionManager` / `SessionJournal`），`bsk/` 侧只声明接口（`bsk/logger.py` 的 `LoggerLike`）并提供 `NULL_LOGGER` 兜底。这条约束有双重来源：插件市场审核要求 logger 必须来自 `astrbot.api`、不得使用内置 logging 模块；而本仓库的分层约束又不允许 `bsk/` 依赖框架。依赖注入是同时满足两者的唯一路径（审核原文亦明确许可该方式）。

```
┌──────────────────────────────────────────────────────────┐
│  main.py   ← 唯一的框架耦合层（必须叫 main.py，见 C1）      │
│  · BskBrowserPlugin(Star)  插件类、配置读取、生命周期钩子   │
│  · @filter.llm_tool 薄适配函数（必须在此，见 C2）           │
│  · 从 astrbot.api 取 logger 与插件数据目录，向下注入         │
│    职责仅：参数解包 → 调用 bsk/ 服务 → 转成框架返回值        │
└───────────────────────┬──────────────────────────────────┘
                        │ 只依赖 bsk/ 包的公开 API
┌───────────────────────▼──────────────────────────────────┐
│  bsk/   ← 纯逻辑层（零 astrbot 依赖，可单测）               │
│  · logger.py    日志接口 LoggerLike + 空实现 NullLogger     │
│  · paths.py     插件数据目录解析与降级（journal/截图共用）   │
│  · models.py    数据模型（Session、Page、Shot、Result）     │
│  · errors.py    bsk 退出码/错误码 → 分类异常 → 中文提示      │
│  · config.py    原始 dict → 强类型 Settings（含校验与兜底）  │
│  · runner.py    子进程调用：超时、编码、退出码、JSON 容错    │
│  · session.py   会话生命周期：umo→session 映射、锁、显式 stop│
│  · pages.py     VOM 语义解析：标题提取、ref 提取、截断       │
│  · shots.py     截图：唯一路径、魔数校验、清理              │
│  · journal.py   会话所有权 journal：原子写、损坏即忽略      │
│  · tools.py     8 个主工具的规格唯一事实源（见 §3.1）        │
│  · service.py   业务编排（tools 的纯逻辑实现，不含装饰器）   │
└──────────────────────────────────────────────────────────┘
```

### 3.1 `bsk/tools.py` 的职责（为什么它必须零框架依赖）

这个模块是 **8 个主工具规格的唯一事实源**，只做四件事，全是纯函数：

| 导出 | 职责 |
|---|---|
| `TOOL_ACTIONS` | 工具名 → 该工具声明的 action 元组（`bsk_session` 3 个、`bsk_page` 5 个、`bsk_inspect` 6 个、`bsk_debug` 24 个、`bsk_load_tools` 1 个、`bsk_interact` 9 个、`bsk_tabs` 6 个、`bsk_assist` 3 个，合计 57 个） |
| `TOOL_SCHEMAS` | 工具名 → 完整 JSON Schema。`action` 的 `enum`、`debug_action` 的 24 项枚举、`device` 的 7 个预设等**都从上面的枚举生成**，不手抄 |
| `TOOL_DESCRIPTIONS` | 工具名 → 给模型看的中文描述。⚠️ 当前注册路径下，模型真正看到的描述来自 `main.py` 的 docstring（`@filter.llm_tool` 只从 docstring 取 `description`，覆写管线只替换 `parameters`）；这个字典目前由 `tests/test_tools.py` 校验"非空且是中文"，是描述的**规约与回归基准**，尚未接线到注册管线 |
| `normalize_args` / `validate` | 参数归一化（同时接受 `snake_case` 与 `camelCase`）与**逐 action 校验**；失败抛 `BskToolError`，`str()` 出来就是给模型看的中文提示 |

**为什么这一层不能依赖 astrbot**：三条理由，缺一条这个设计就不成立。

1. **它是被覆写进框架的载荷本身。** `main.py` 只是把 `TOOL_SCHEMAS` 的值 deepcopy 到已注册工具上（§5 D8）。如果把 schema 的构造写进 `main.py`，那 `main.py` 就从「薄适配层」变成「规格 + 适配混在一起」，C2 逼出来的分层立刻失效。
2. **AstrBot 的 Schema 方言不是通用 JSON Schema，必须能独立验证。** 框架的类型白名单是 `{string, number, object, array, boolean}` —— **没有 `integer`**，因为 AstrBot 的 `spec_to_func` 走的是它自己的那套转换。所以本模块里所有整数语义的参数（`limit`、`max_depth`、`timeout_ms`、`width`…）一律声明成 `"number"`，整数性与范围改由 `validate` 在运行时检查。这类"方言"约束只有在能脱离框架反复跑单测时才好守。
3. **per-action 校验天然是纯逻辑。** DSH 的结构是「公共 schema 只有 `action` 必填、其余参数是扁平并集，真正的必填/互斥/范围校验在 handler 里做」。本插件复刻了这个两层结构，但把第二层下沉成了纯函数 —— 于是 57 个 action 的校验分支可以用 `tests/test_tools.py`（103 个用例）直接覆盖，不需要启动 AstrBot、不需要真实浏览器。

**为什么 `required` 只写 `action`**：AstrBot 的 `spec_to_func` 只构造 `{type: "object", properties}`，**不生成 `required`**。我们走的是「装饰器注册后覆写 `parameters`」这条路（§5 D8），覆写后的 schema 会被原样交给 provider，所以这里写的 `required: ["action"]` 是**真的会传给模型**的（DSH 侧同样如此）。其余参数的必填仍然只能靠 `validate` 在运行时报中文错 —— AstrBot 读不懂 per-action 的必填。

**归一化约定**：`normalize_args` 把键名统一成内部 `snake_case`，同时接受 DSH 文档里的 `camelCase` 与连字符写法（`scroll-to` → `scroll_to`）。它**不抛异常** —— 非法输入原样跳过，真正的报错交给 `validate`，这样错误消息才能说清是哪个参数。

**静态保障**：`bsk/` 零 astrbot、零 `logging` 这条约束由 `tests/test_logger_injection.py` 用 AST 扫描钉死（不是关键字搜索），`verify_release_ready.py` 里另有一道同样的检查。本模块只用标准库的 `json` 与 `re`（都是纯 C 模块，不参与 `bsk.logger` 的注入链）。

**数据落盘位置**：会话所有权 journal 与截图默认都落在**插件数据目录** `data/plugin_data/astrbot_plugin_bsk_browser/` 下（由 `bsk/paths.py` 统一解析，见 §4.7）。该目录由 `main.py` 通过 `StarTools.get_data_dir("astrbot_plugin_bsk_browser")` 取得后注入；取不到时逐级降级，最终退回系统临时目录，**任何一步都不抛异常**（它在插件加载路径上，抛异常等于插件加载失败）。

**为什么这样分**：
- `C2` 强制工具函数必须在 `main.py`。若把业务逻辑也写进去，`main.py` 会变成数千行巨石（v0.1.0 的骨架就已经是 44KB 单文件；当前 `main.py` 是 2307 行 / 约 114KB，其中相当一部分还是**不可避免**的 16 个工具函数本身）。
- 因此让 `main.py` 只做薄适配：每个工具函数 5-15 行，负责"取参数、调 service、包结果"。16 个工具的 schema、归一化、逐 action 校验全部下沉到 `bsk/tools.py`（§3.1）—— 那是本次改造把 `main.py` 的体积增长摁住的关键：**多出来的 8 个主工具几乎没有给 `main.py` 增加规格代码**，只增加了分派与渲染。
- 真正的逻辑在 `bsk/service.py`（当前 3374 行）及其依赖，不 import astrbot，所以可以直接 `pytest` 跑，不需要启动 AstrBot。`bsk/` 包当前合计 11761 行。
- 同理 `bsk/` 也不 import 内置 `logging`：日志能力同样通过注入获得，未注入时走 `NULL_LOGGER`。这让"脱离框架单测"这条价值不被日志需求侵蚀 —— 单测里既不产生日志输出，也不需要搭建日志设施。

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

退出码映射（实测 6 档）：`0` 成功 / `1` 用户错误 / `2` 协议传输 / `3` 浏览器CDP / `4` 超时 / `5` 版本不匹配。

### 4.2 `bsk/runner.py`

唯一与子进程打交道的地方。

```python
class BskRunner:
    def __init__(self, bsk_path: str, default_timeout: float): ...
    async def run(self, args: list[str], timeout: float | None = None) -> BskResult: ...
```

必须处理的坑（全部实测确认）：
1. `asyncio.create_subprocess_exec(*args)` —— 列表参数，不过 shell（无注入面）
2. 超时必须包住 `communicate()` —— Windows 下 daemon 继承管道句柄可能导致永不返回
3. 超时后：先关 stdin 给 15 秒宽限（Windows 取消语义），再 `kill()`
4. stdout/stderr 显式 `.decode("utf-8", errors="replace")` —— 见 C9
5. 错误 JSON 走 stdout 不是 stderr；clap 参数错误是 stderr 纯文本 → 解析必须容错
6. 退出码非 0 即失败，不能靠解析 JSON 判成败

### 4.3 `bsk/session.py`

```python
class SessionManager:
    async def acquire(self, key: str) -> BskSession: ...   # 取或建
    async def release(self, key: str) -> None: ...         # 显式 stop
    async def release_all(self) -> None: ...               # terminate 用
```

**必须遵守**：
- `session stop <ID>` 的 id 是位置参数（唯一例外，其余命令用 `--session`）
- 每个 key 一把 `asyncio.Lock`，禁止同 session 并发（bsk 同时只允许 1 个 in-flight，否则 `session_busy`）
- 绝不使用 `session stop --all` —— 会停掉别的程序（如 DSH）创建的会话
- 会话失效靠"用失败触发重建"（`not_found` → 重建 → 重试 1 次），不要每次操作前先探测
- `terminate` 里必须 `try/finally` 全部 stop，否则留下 Agent Window 打扰用户
- `session_busy` 等 100ms 重试一次；`outcome_unknown` 禁止重试并标记会话为不确定态

### 4.4 `bsk/pages.py`

`observe` 返回的是带标记的缩进文本树，不是 JSON 结构：

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

**注意**：`observe` 没有独立 `title`/`url` 字段，只能正则提取。
**注意**：`snapshot` 与 `observe` 实测输出逐字节相同且都不带截图 → 只调 `observe`。

### 4.5 `bsk/shots.py`

- 必须自己指定绝对路径并保证唯一（含 session_id + 时间戳），否则并发互相覆盖
- `--out` 会覆盖已有文件
- 拿到路径后三重校验：文件存在、大小 == `byte_size`、前 8 字节魔数（某些 Chromium 构建请求 png 却返回 JPEG）
- 定期清理，避免磁盘无限增长
- 给 LLM 前考虑降采样（1850×1208 的原图很贵）

### 4.6 `bsk/config.py`

把 AstrBot 传来的原始 dict 转成强类型 `Settings`，对每一项做类型校验和兜底（配置可能被用户填成任何东西）。

`Settings` 另有一个**注入项**（不是用户配置项）`data_dir`：插件数据目录，由 `main.py` 传入。
空串表示"未注入"，此时 journal 与截图走临时目录降级。它**不**写进 `_conf_schema.json` ——
用户不该也无法在配置页填它。`parse_settings(raw, *, data_dir="")` 以关键字参数接收。

用户显式配置的 `screenshot_dir` / `journal_path` **优先级最高且不变**：填了就一律用用户填的，
注入的 `data_dir` 不得覆盖它。

### 4.7 `bsk/logger.py` 与 `bsk/paths.py`

这两个模块是本次为过审新增的，都是纯标准库、零 astrbot 依赖。

`bsk/logger.py` —— 日志接口的接缝：

```python
@runtime_checkable
class LoggerLike(Protocol):          # debug/info/warning/error/exception
    def info(self, msg: object, *args: Any, **kwargs: Any) -> None: ...

class NullLogger:                    # 什么都不做，也永不抛异常
    __slots__ = ()
    ...

NULL_LOGGER = NullLogger()           # 全局共用的兜底实例（无状态）
```

- 调用约定与标准库一致：**惰性 %-格式化**（`logger.info("会话 %s 已重建", key)`）。
  不能改成 f-string —— 那样日志级别被关掉时仍会白做一次拼接与格式化。
- `exception()` 必须在：`main.py` 的兜底分支用它，靠的是"记录当前异常"这一语义。
- 未注入时调用点无条件走 `NULL_LOGGER`，而不是在几十处写 `is not None` 判断 ——
  漏掉一处就是一个 `AttributeError`，且偏偏出现在最不该出问题的降级路径上。

`bsk/paths.py` —— 落盘位置的统一解析：

```python
def resolve_data_dir(data_dir, logger=None) -> Path:      # 可用则用之，否则降级
def default_journal_path(data_dir, logger=None) -> Path:  # <data_dir>/sessions.json
def default_shot_dir(data_dir, logger=None) -> Path:      # <data_dir>/shots
```

降级顺序（**必须保留容错**，这是审核明确要求的）：

1. `data_dir` 非空、可创建、可写 → 用它（正常路径，`data/plugin_data/astrbot_plugin_bsk_browser`）；
2. 否则 → 退回系统临时目录下的 `<系统临时目录>/astrbot_bsk_browser`（journal 在其下，
   截图在 `<系统临时目录>/astrbot_bsk_browser/shots`），记一条 warning；
3. **任何一步都不抛异常** —— 它在插件加载路径上，抛异常等于插件起不来。

第 2 条为什么不能删：`StarTools.get_data_dir()` 失败时会抛异常，而 journal 与截图
都必须有个可写的落点，"数据目录拿不到"不该升级成"插件不可用"。降级只影响数据的
存放位置（临时目录会被操作系统清理），不影响功能。

`data_dir` 传非字符串（数字、`None`、列表……）或非法路径（含空字节、Windows 保留
设备名）时同样按"未注入"降级，而不是抛 `TypeError`。返回的路径**总是绝对的**：
相对路径会随工作目录漂移，而 journal 要跨进程读。

---

## 5. 关键设计决策

### D1：注册路径选 `@filter.llm_tool`（而非 `context.add_llm_tools`）

- 前者让框架完成 handler 注册与插件实例绑定，单一事实来源，不易写错；
- 后者需手写 JSON schema，且要自己管 `handler_module_path` 与 `functools.partial` 的实例绑定管线。
- 代价：工具函数必须在 `main.py`（C2），已由分层设计消化。
- 8 个主工具的 schema 表达力不够（装饰器路径丢弃 `enum`），由 D8 的覆写管线补齐。

### D2：权限默认仅管理员，且可调（用户要求 #5）

- 插件自己实现 `admin_only`（默认 `true`），读插件配置，可在 AstrBot 插件设置页直接改；
- 每个工具函数入口统一调用一个 `_guard(event) -> str | None`；
- 额外支持 `allowed_users`（用户 ID 白名单），便于"只给某个人用"；
- 同时兼容 AstrBot 原生的 `tool_permissions`（WebUI → 扩展组件）机制，两者取严。

### D3：`evaluate` 单独成工具、默认关闭、强制管理员（原为"不做 evaluate"，v0.1.0 追加）

`evaluate` 能让模型在用户已登录页面执行任意 JS。能力保留但默认关闭，
且独立成一个工具而不是并进 `bsk_act`，具体决策见 §5 D7。

仍然不做的事：不把它作为 `bsk_act` 的一个 action（那样会绕过独立开关与
AstrBot 原生的 `tool_permissions`），也不给它任何"自动降级"路径。

### D4：会话键策略

`umo`（每个聊天会话一条）为默认，可选 `user`（每人一条）。键提取做三级兜底：`unified_msg_origin` → `session_id` → `sender_id`。

### D5：错误一律转成"模型能看懂的中文"

工具返回给 LLM 的字符串必须包含下一步该怎么做（例如"会话已过期，已自动重建，请重试"），而不是抛裸异常。

### D6：超时采用「取较大值 + 框架上限钳制」的规则

**规则一句话**：`command_timeout_sec` 是所有 bsk 命令的超时；每个命令的内置值是下限，
整页截图另有专门的可配置项；最后统一被框架上限钳一次。

```
最终超时 = min( max(内置下限, command_timeout_sec), 框架上限 - 5s )
整页截图：内置下限换成 settings.fullpage_timeout_sec（默认 120，范围 30–600）
```

- 内置下限 = "这个命令至少需要多久"。例如 `navigate` 必须 大于 bsk 自身的 `--timeout`
  30s，否则我们会先把它掐掉、而它正要成功返回。
- 用户配置 = "我愿意等多久"。
- 取较大值，两者都不被违背。
- 最后与"框架上限 − 5s"取较小值：保证插件在框架动手之前自己超时（见下方"框架上限"）。
  框架上限读不到时不钳制 —— 拿不到事实就不该凭猜测缩短用户的等待。

| 命令 | 内置下限 |
|---|---|
| status / browsers / session list / console / network | 5s |
| `session request --help`（能力探测） | 10s |
| `session request` 的 prepare / claim / cancel / query | 30s（它们不建窗口、不导航，只改 daemon 侧记账） |
| observe | 15s |
| session start | 30s |
| navigate | 45s（必须 > bsk 自身默认 30s） |
| screenshot 视口 | 30s（固定，不读 fullpage 配置） |
| screenshot --full-page | `fullpage_timeout_sec`（默认 120s，用户可调 30–600） |

**为什么整页截图单独一项**：它的耗时与其余命令完全不在一个量级（实测 2.9–11.7s，
其余命令多在 1s 内），塞进 `command_timeout_sec`（上界 110s）既不够用、又会让用户
为了截图把"所有命令的超时"一起拉长。独立一项可以只调它。视口截图刻意不读这一项：
实测只要 0.12s，跟着变成 120s 属于误伤。

为什么不做更复杂的规则（例如"快命令取 min、慢命令取 max"）：本插件的使用者是
编程新手，配置项的行为必须能用一句话说清。复杂规则会带来解释成本和"为什么调了没用"
的困惑 —— 而这正是改进前的老问题。

#### 框架上限（`tool_call_timeout`）与钳制

AstrBot 的 `tool_call_timeout` 默认 120 秒（源码 `core/agent/run_context.py:19` 与
`core/config/agent_runner.py:33`；本机配置文件实测值也是 120），可调项为
`agent_runner.config.misc.tool_call_timeout`。到点后框架抛
`tool <name> execution timeout after N seconds.`（`astr_agent_tool_exec.py:691-726`）。
**插件能读到它**：`Context.get_config()`（`core/star/context.py:597`）→
`bsk.config.read_framework_tool_timeout`（鸭子类型下探，任何异常都降级成 None）。

**钳制的理由**：插件的全页截图预算与框架默认上限都是 120 秒。若不钳制，用户看到的
会是框架抛的英文 `execution timeout`，而不是插件精心写的中文提示，并且"会话可能
留下未完成状态"这件事被完全掩盖。钳制后（默认组合 → 115 秒）超时由插件先报出来。
安全余量 5 秒的理由：框架从它开始等的那一刻计时，而 bsk 返回后我们还要校验截图、
渲染中文、序列化结果、交给框架发图片 —— 这些都在同一个窗口里，留余量才不会正好撞边界。

⚠️ 不要把这个钳制说成"必须两处一起调大才能用全页截图"。实测数据（见 §6.2）：
长页面全页截图 11.72 / 11.11 / 10.91 秒，短页面 2.91 秒，距 120 秒上限约 1/10。
默认配置下整页截图就能用，什么都不用改。只有极慢的页面/网络才需要同时放宽两处，
且顺序是"先调大框架、重启，再调大插件"——否则只调插件不会更久（会被钳住）。
这一点必须写进 README 的已知限制里，否则用户要么白改配置，要么以为插件有 bug。

### D7：`evaluate` —— 独立工具、默认关闭、强制管理员盖过 `admin_only`

`bsk evaluate` 在用户已登录的页面里执行任意 JavaScript。这是 bsk 最强的
能力（读 DOM、改页面、带 cookie 调 `fetch`），但它的风险不是 click/fill
的"更强版本"，而是另一种性质的东西：

| | click / fill / press | evaluate |
|---|---|---|
| 用户能否看见 | 能，动作显示在浏览器窗口里 | 不能，脚本静默运行 |
| 能读到什么 | 页面上显示的内容 | 页面上一切（含 token、隐藏字段、localStorage） |
| 能发请求吗 | 只能通过点按钮触发 | 可以直接 `fetch`，带登录态 |
| 人工兜底 | 页面的 confirm 弹窗会拦住 | 弹窗被自动确认（实测 `handled: "accepted"`） |

因此有三个决策，每个都对应上表的一行：

**为什么单独成工具（而不是 `bsk_act` 的一个 action）**

1. 才能被单独禁用。动作混在一个工具里，用户就没法"保留点击、禁掉执行脚本"
   —— 只能整块开或整块关，等于逼用户在做不到精细控制时干脆全开。
2. 才能被 AstrBot 原生的 `tool_permissions` 单独控制（WebUI → 扩展 → 组件）。
   框架那一层是按工具名授权的；`evaluate` 藏在 `bsk_act` 里就自动继承了
   `bsk_act` 的授权，管理员在框架侧无法把它单独摘出去。
3. 权限判定顺序才能不同。`bsk_act` 走 `_denied()`；`evaluate` 要在它之前
   多插两道判（见下）。

**为什么默认关闭（`enable_evaluate=false`）**

- 升级不该凭空多出高危能力。用户装 0.1.0 时心里那笔账是"机器人能点页面"；
  如果新版本默认把"在你邮箱里跑任意脚本"一起打开，那是在用户不知情的情况下
  扩大了授权。新增高危能力必须显式开启，这个默认值本身就是一道同意。
- 它也不复用 `enabled` 总开关，理由同上：总开关的语义是"插件是否工作"，
  不是"是否开放最高危能力"，两者混在一起会让用户为了关掉一个能力而停掉整个插件。

**为什么"强制管理员"要盖过 `admin_only=false`（本决策的重点）**

`admin_only=false` 的语义是"我愿意把看得见的浏览器操作开放给其他人"
（群里的人点按钮、填表单，用户全程能在窗口里看着）。而执行脚本是静默的。
如果让这个粗粒度的宽松开关顺手把最高危能力一起放开，就产生了一条极难察觉的
权限放大路径：管理员只想"让群里的人也能查网页"，结果同时交出了
"在已登录页面里跑任意脚本"。

所以实现上 `_evaluate_denied()` 的判定是有序的三道（顺序即设计）：

```
1. enabled            — 总开关
2. enable_evaluate    — 独立开关（默认关闭）
3. evaluate_require_admin + is_admin  ← 这一步必须在 _denied() 之前
4. _denied()          — admin_only / allowed_users 等既有规则
```

第 3 步放在第 4 步之前、而不是把两者写进同一个条件表达式，是为了让
"`admin_only=false` 不能绕过它"成为结构性保证而不是巧合：
`_denied()` 里那条 `if self.settings.admin_only and not is_admin` 根本没机会执行。
`allowed_users` 白名单同理 —— 它对别的工具是"准入"，对 evaluate 不是。

放开需要两次独立决定：既要 `enable_evaluate=true`，又要
`evaluate_require_admin=false`。这个"摩擦力是特性"的设计已在
`tests/verify_evaluate_gate.py` 里按分支钉死（分支 2、2b 是核心用例）。

**超时**：`TIMEOUT_EVALUATE = 45s`，遵守 D6 的单一规则。必须大于 bsk 自身的
`--timeout`（默认 30s，实测帮助文本），否则我们会先把它掐掉 —— 与
`navigate` 同一条理由。我们不给 bsk 传 `--timeout`，好让"bsk 内部超时"
与"外层兜底超时"保持明确的先后关系。

**实现上最容易错的一点（已用测试钉死）**

JS 抛异常时 bsk 的退出码仍然是 0，失败只体现在返回 JSON 的 `ok: false` 里。
`SessionManager.execute` 是按退出码判成败的（对 bsk 其它命令都正确），
所以这一步必须在 `service.evaluate()` 里自己做：

```python
evaluation = EvaluateResult.from_json(result.data)
if not evaluation.ok:           # ← 不能省
    raise BskError(..., code="evaluate_js_error")
```

只信退出码的后果是"把失败当成功"，然后拿一个不存在的值去回答用户 ——
而模型不会知道自己错了。实测三种形态（全部 `exit=0`）：
`throw new Error('boom')`、`ReferenceError`、`SyntaxError`。

其它实测约束（写进代码注释与 `bsk/models.py`）：

- `EXPRESSION` 是位置参数；
- 求值成 `undefined` / `null` 时 `value` 字段整个消失（要用 `has_value`
  区分"没有值"和"值是 null"）；
- 没有弹窗时 `dialogs` 字段整个消失；
- 返回值可以是任意大的 JSON（实测 2000 元素数组 = 32947 字符），
  必须截断 —— 复用 `max_page_chars`，不新增配置项。

### D8：8 个主工具的注册管线 —— 装饰器注册 + `initialize()` 里覆写 `parameters`

**结论（一句话）**：用 `@filter.llm_tool` 让框架完成 handler 注册与实例绑定，
然后在 `initialize()` 里把已注册工具的 `parameters` **整体替换**成 `bsk/tools.py`
里的完整 schema。

**为什么不能只用纯装饰器**（路径甲）：装饰器从 docstring 的 `Args:` 段生成 schema，
那条路径**支持的 `type` 只有 5 种，且会丢弃 `enum`**。而本插件的 8 个工具全靠
`action` 的枚举告诉模型有哪些动作可选 —— `bsk_debug` 的 `action` 更是有
24 个取值。丢掉 `enum` 等于把「57 个 action 的多动作工具」降级成「参数含义不明的
单个函数」，这次移植的核心价值就没了。

**为什么不用手工 `add_func` + 手工注册 handler**（路径乙）：那要自己接
`star_handlers_registry` 与 `functools.partial` 的实例绑定管线，脆弱且易错；
路径甲已经把这段（框架内部、有版本风险的部分）交给框架自己维护了。

**为什么覆写必须发生在 `initialize()` 里**（本决策最容易做错的一点）：

1. **插件重载会把 `FuncTool` 整个重建。** AstrBot 重载插件时会先把该插件的模块从
   `sys.modules` 里清掉（`star_manager._purge_modules`），再 `__import__` 重新执行
   `main.py` —— 于是装饰器**再跑一遍**，而 `llm_tools.add_func` 内部是
   `self.remove_func(name)` 之后 `spec_to_func(...)` 追加一个**全新的 `FuncTool`**，
   它的 `parameters` 来自 docstring。**任何在模块级（import 期）写下的补丁都会随旧对象一起被丢掉。**
   而 `initialize()` 是每次加载都会执行的那个点，写在这里才覆盖得住。
2. **`initialize()` 早于任何一次 `get_full_tool_set()`。** `_PermissionGuardedTool`
   在构造时对 `parameters` 做的是**快照**（`parameters=getattr(tool, "parameters", {})`），
   不是实时视图；晚于它覆写就没有意义了。
3. **覆写是幂等的**：每次都是整体替换而非追加，重复 `initialize()` 不会累积。

**覆写前必须先过一道 schema 合法性预检**（`_schema_is_sane`）。原因是实测出来的：
`tool.parameters = {...}` 这个赋值**不触发** pydantic 校验（校验器是
`model_validator(mode="after")`，只在构造 `FunctionTool(...)` 时跑）。非法 schema 要到
**下一次** `get_full_tool_set()` 构造 `FunctionTool(...)` 时才炸，而那条路径被
`internal.py` 兜住、往聊天里发一句 "Error occurred while processing agent request: ..."
—— 也就是说**一条 schema 笔误会让机器人对每条普通消息都报错**，远不止影响浏览器工具。
所以不合法就跳过该工具的覆写、保留 docstring 推出的基础 schema（那个永远合法）并告警：
代价是那个工具少了 action 枚举，远比整个机器人每条消息都报错轻。**刻意不引入
`jsonschema` 依赖** —— 本插件要求纯标准库。

**handler 签名契约**：8 个新工具一律写成

```python
@filter.llm_tool("bsk_session")
async def bsk_session(self, event: AstrMessageEvent, **kwargs):
    return await self._dispatch("bsk_session", event, kwargs)
```

即只声明 `event` + `**kwargs`。框架的调用方式是 `handler(event, *args, **kwargs)`
（`astr_agent_tool_exec.py:763`），其余参数按**形参名**注入；若模型传了签名里没有的
参数会 `TypeError`，框架随即抛 `Tool handler parameter mismatch`。用 `**kwargs`
接住全部参数，多传的不会导致调用失败 —— 这与 DSH 的「implicit open object root」
同构，真正的校验由 `bsk/tools.py` 的纯函数做（§3.1）。

**docstring 的作用被降低但没有消失**：覆写之后 `Args:` 段不再是 schema 来源，
但 `docstring.description`（`Args:` 之前的部分）仍是工具的初始描述，也是覆写失败时
模型唯一能看到的说明 —— 所以每个 docstring 里仍写清 action 取值，并控制在 300 字符
以内（token 成本）。

**旧 8 个工具不走这条管线**，它们就是普通的装饰器工具，schema 由 docstring 生成，
与 0.1.x 完全一致。它们由配置项 `legacy_tools` 与 `legacy_fringe_tools` 两级控制（§5 D9）。

### D9：旧工具的两级开关（`legacy_tools` / `legacy_fringe_tools`）—— 用运行期 `active` 而非框架的停用 API

8 个主工具落地后，16 个工具并存会降低模型的选择准确率，同时 8 个旧工具的说明是
**每一轮对话都要付的固定开销**。所以给旧工具两级开关，让用户自己决定带不带兼容层跑：

| 配置 | 默认 | 管什么 | 停用后剩下 |
|---|---|---|---|
| `legacy_tools` | `true` | 全部 8 个旧工具 | 8 个主工具（`bsk_debug` 按需加载时是 7 个）+ `bsk_evaluate` |
| `legacy_fringe_tools` | `false` | 其中"有等价新工具"的 3 个：`bsk_open` / `bsk_read` / `bsk_act` | 常用的 4 个继续注册 |

**为什么 `legacy_tools` 默认必须是 `true`**：AstrBot 的插件配置是 merge 语义（缺失键插默认值、
已有的非 `None` 值原样保留）且加载时立即写盘。默认 `true` → 老用户升级后一切照旧；
默认 `false` → 等于**静默关掉** 7 个旧工具（`bsk_evaluate` 除外），正在用旧写法的提示词会毫无预兆地发现工具"没了"。

**为什么 `legacy_fringe_tools` 默认是 `false`**：这是本次升级唯一会让老用户感知到的默认变化，
之所以敢默认关，是因为那 3 个都有等价的新工具（`bsk_open` → `bsk_page` + `bsk_inspect`、
`bsk_read` → `bsk_inspect`、`bsk_act` → `bsk_interact`），关掉不减能力；而常用的 4 个
要么新工具做不到（`bsk_screenshot` 会真的发图片、能整页截图），要么是诊断入口
（`bsk_status`、`bsk_logs`），所以留在 `legacy_tools` 这一级。两个开关都开时才是"全兼容"形态。

**为什么直接设 `tool.active = False`，而不是调框架的 `deactivate_llm_tool_async`**：
后者会把这个名字写进**持久化**的 `inactivated_llm_tools`（全局 SharedPreferences）。
用户只是临时关掉 `legacy_tools` 试试，却会在全局配置里留下永久痕迹，**卸载插件也带不走**
—— 下次装回来那 8 个工具还是关着的。直接设 `active` 只影响本次进程，随插件重载自然恢复。

**两个已知边界**（`_conf_schema.json` 的 hint 里已如实写明）：

1. 用户若在 AstrBot WebUI 的「扩展 → 组件」里手动关过某个旧工具，
   `star_manager` 每次加载都会重算 `ft.active = not plugin_disabled and ft.name not in inactivated_llm_tools`
   —— 那一步在 `_deactivate_legacy_tools()` **之后**跑，所以框架侧的关闭状态始终优先；
2. 只在插件加载时执行一次，改配置需要重新加载插件。

**`bsk_evaluate` 不在停用范围内**（`LEGACY_TOOL_NAMES` 里有它，但循环中显式跳过）：
它是**独立的高危开关**，由 `enable_evaluate` 控制（§5 D7）。把它绑到 `legacy_tools`
上会让用户误以为"关掉旧工具"就等于"关掉执行脚本"，而实际上它归另一项管。

**量化收益**（实测，本机 AstrBot 4.28.1，把这 12~16 个工具按 `ToolSet` 序列化成 provider
真正收到的那份 JSON，取 `len(json.dumps(..., ensure_ascii=False))`；三种 provider 格式分别计）：

| 配置 | 注册的工具数 | Google | Anthropic | OpenAI |
|---|---|---|---|---|
| 本仓库 `10dd1f7`（本次改造前：6 主 + 8 旧，`bsk_inspect` 含 24 个调试参数） | 14 | 18914 | 19097 | 19417 |
| `legacy_tools=true` + `legacy_fringe_tools=true` + `lazy_debug_tool=false` | 16 | 20178 | 20365 | 20749 |
| `legacy_tools=true` + `legacy_fringe_tools=true` | 15 | 15241 | 15426 | 15778 |
| **默认**（`legacy_tools=true`、fringe 关、调试按需加载） | **12** | **13300** | **13413** | **13717** |
| `lazy_debug_tool=false`（`bsk_debug` 一直注册） | 13 | 18237 | 18352 | 18688 |
| `legacy_tools=false` | 8 | 11669 | 11674 | 11914 |

几个能直接读出来的结论：

- **默认比改造前省 5614 / 5684 / 5700 字符（约 29%）**；
- **关掉 `legacy_tools` 相对默认再省 1631 / 1739 / 1803 字符**，而不是省下"8 个旧工具"的
  全部开销 —— 因为默认状态下 fringe 那 3 个本来就没注册；「8 个旧工具全开」与「全关」之间的
  差额是 3572 / 3752 / 3864 字符（`_conf_schema.json` 的 hint 里写的"约 3600 字符"是这个口径）；
- **打开 `legacy_fringe_tools` 多付 1941 / 2013 / 2061 字符**（hint 里写的"约 1900"）；
- **关掉 `lazy_debug_tool` 多付 4937 / 4939 / 4971 字符**（hint 里写的"约 5000"）。

注意 `legacy_tools=false` 后仍是 **8 个而不是 7 个**：多出来的那个正是
不受本项影响的 `bsk_evaluate`。

### D10：与 DSH（`dsh-plugin-browserskill`）的有意差异

8 个主工具的目标是"使用体验等价"，但**不是逐字节复刻**。下面每一条差异都是
有意为之，理由写在第三列 —— 不是没做完。

| 项 | DSH | 本插件 | 理由 |
|---|---|---|---|
| `request_id` / 可恢复启动 | 支持（模型可传的参数） | **支持**（插件驱动，模型看不到这个参数） | 语义已实测确认（`session request` 的 prepare 预约 / claim 认领 / cancel 关闭 / 未预约令牌返回 `unknown`）。本插件把它做成插件内部机制而不是工具参数：令牌由插件生成、**写前落盘**、启动后认领，只由 `enable_request_id` 控制。理由见 §5 D12 下方 —— 令牌一旦由模型填，恢复路径的正确性就取决于模型是否填对值，而它只在启动失败这条罕见路径上起作用 |
| `current` 会话回退 | `stop` 后回退到最近的 active 会话 | **不回退** | 有意分歧。自动回退意味着"关掉 A 之后，下一个操作打在 B 上"，而 B 是用户可能没意识到还开着的会话。不回退会让模型明确地重新 `start`，代价是一次多余调用，换来的是不会误操作另一个会话 |
| 会话状态机 | `starting` / `active` / `cleanup` 三态 | 只有 `owns()` | 未实现"半途失败的 start"清理。AstrBot 侧没有对应的生命周期事件可挂；会话泄漏由 journal + `recover_orphans()` 在下次启动时兜底（§4.3 与 §6.1），启动超时留下的孤儿窗口另由可恢复启动的令牌路径兜底（§5 D12） |
| SSE 实时观察 / 侧边栏 UI / 缩略图 | 有 | **无** | 展示层能力。AstrBot 没有 DSH 的 `webServer` 路由 seam 与客户端插件体系，无承载之处 |
| `lazyTools`（大工具按需注册） | 有（skill 调用后注册） | **支持**（模型调用 `bsk_load_tools` 后加载 `bsk_debug`） | 触发方式不同：DSH 靠 skill 触发，本插件靠一个常驻的小工具触发；本插件改的是**当轮** `ProviderRequest.func_tool`，同一轮的下一次请求就生效，不必等下一步操作（§5 D11）。它替换掉了早先"只能在插件加载期开关"的权宜方案 |
| `bsk_evaluate` | **不暴露** | **保留**（独立开关 + 强制管理员） | 本插件**超出** DSH 的部分。这是 0.1.x 的既有功能，且风险已由 `enable_evaluate` + `evaluate_require_admin` 两道独立闸单独门控（§5 D7）。移除它等于回退既有能力 |
| `bsk_screenshot` 的 `full_page` | 未暴露 | **保留** | 同上，既有功能，且严格更强（能截整个长页面）。注意新工具的 `screenshot` 是 DSH 语义，没有整页能力 |
| 新 `screenshot` 是否把图片发给用户 | 会发 | **不发**（只回文字） | 新 handler 返回 `str`，不是 async generator，拿不到 `yield event.image_result(...)` 那条路。这是本次改造里**唯一的功能回退点**，因此旧工具 `bsk_screenshot` 必须保留 |

**反向说明（本插件比 DSH 弱、且短期内不打算补的）**：上面第 3 行属于功能性缺口；
第 4 行属于展示层缺口，**不影响工具功能的等价性**。

### D11：`bsk_debug` 的按需加载（`lazy_debug_tool`）—— 改**当轮** `req.func_tool`

`bsk_debug` 的 schema 有 4431 字符（24 个 action 的参数说明），而绝大多数对话并不调试网络。
所以默认（`lazy_debug_tool=true`）不把它注册给模型，改由常驻的小工具 `bsk_load_tools`
在模型真的需要调试时把它加载进来。

**完整链路**（行号为本机 AstrBot 4.28.1 源码实测）：

| 位置 | 做什么 |
|---|---|
| `astr_main_agent.py:1547` | `event.set_extra("provider_request", req)` —— 把**同一个** `ProviderRequest` 对象存进事件 extra |
| `astr_main_agent.py:1753` | `agent_runner.reset(request=req, ...)` —— 同一个对象又交给 runner |
| `runners/tool_loop_agent_runner.py:234` | `self.req = request` |
| `runners/tool_loop_agent_runner.py:504-506` | `payload = { ... "func_tool": self._func_tool_for_provider(), ... }` |
| `runners/tool_loop_agent_runner.py:678` | `return self.req.func_tool` —— **每步现读**，不是初始化时的快照 |
| `runners/tool_loop_agent_runner.py:1096` | 主循环 `while not self.done() and step_count < max_step:` |

三条要点：

1. **handler 里改 `req.func_tool`，同一轮的下一步就生效。** `bsk_load_tools` 的 handler 用
   `event.get_extra("provider_request")` 取回那个对象，把 `bsk_debug` 的 `FunctionTool`
   加进它的 `func_tool`；runner 下一步构造 payload 时现读 `self.req.func_tool`，于是**下一次**
   LLM 请求就带上了 `bsk_debug`。不需要等下一轮对话，也不需要任何框架 hook。
2. **不会污染别的会话。** `ProviderRequest` 是每轮请求新建的，`func_tool` 是该请求私有的；
   用户的下一条消息会重新构造请求，`bsk_debug` 自然回到未加载状态 —— 这正是要的语义
   （"只在当前这轮有效"）。所以**只**往 `req.func_tool` 里加东西，绝不碰全局 `active`。
3. **为什么用全局 `active=False` + 当轮加载，而不是一直开着。** `bsk_debug` 在
   `lazy_debug_tool=true` 时是 `active=False` 的；`ToolSet` 的三个 schema 序列化器都不按
   `active` 过滤，`llm_tools.get_func` 对未激活的同名工具也会退化返回一个 —— 所以"加进当轮
   `req.func_tool`"就等于对它**临时激活一轮**。全局关掉是为了"不默认付那 4431 字符"，
   当轮加进去是为了"要用时能用"，两者不在一个层次上。设置 `active` 用的是直接赋值而不是
   框架的 `deactivate_llm_tool_async`，理由与旧工具开关相同（§5 D9）：后者会写**持久化**的
   全局配置，卸载插件也带不走。

**为什么加载入口必须是另一个工具**：不能把"加载"做成 `bsk_debug` 自己的一个参数 ——
那是循环依赖，模型得先"看见" `bsk_debug` 才能传参给它。所以入口只能是另一个**常驻**的
小工具（181 字符的 schema），它自己不操作浏览器，只负责把大工具加进当轮请求。

**一个已知边界**（源码阅读，未实测）：AstrBot 还有 `tool_schema_mode="skills_like"` 模式，
该模式下 runner 初始化时会把 `self.req.func_tool` **替换**成一份轻量工具集
（`runners/tool_loop_agent_runner.py:298-306`）。加载逻辑作用在同一个 `self.req.func_tool`
对象上，所以 `bsk_debug` 会带着**完整 schema** 进入当轮请求 —— 功能上没问题，只是这一个
工具不吃轻量化省下的 token。默认模式是 `full`，不走这条路径。

### D12：可恢复启动（`enable_request_id`）—— 写前落盘的一次性令牌

**要修的缺陷**（实测确认）：`session start` 超时会让窗口成为永久孤儿。

```
fresh = await self._start(...)     # ← 超时在这里抛异常
self._journal_add(fresh)           # ← 永远执行不到
```

窗口可能已经开出来了，但调用方拿不到 `session_id`，于是 journal 里没有任何记录，
`recover_orphans()` 也够不着它 —— 桌面上留下一个既关不掉、也清理不掉的浏览器窗口
（重启 AstrBot 同样带不走，因为插件自己都不知道它存在）。

**对策**：把"先启动、后落盘"倒过来。核心不变式是「**绝不带着 `--request-id` 发出 start，
却没有持久化的令牌**」—— 令牌是 start 回执丢失后唯一能把窗口找回来的东西，落盘晚一步就等于没落：

```
写前落盘令牌 → session request <令牌> --prepare → session start --request-id <令牌> → --claim
```

任一步失败时的裁决（`bsk/session.py` 的 `_restart`）：

| 失败点 | 处理 | 理由 |
|---|---|---|
| 令牌写不进 journal | 不带令牌走普通启动（= 旧行为），并记 `request_write_ahead_failed` | 拒绝启动会让"数据目录不可写"这种环境问题把整个浏览器功能瘫痪；要守的不变式是"带了令牌就必须落了盘"，既然没落盘就不带 |
| `--prepare` 失败 | `--cancel` + 清记录 + 抛错 | 还没发 start，窗口一定不存在（实测 `--prepare` 返回 `session: null`） |
| `start` 抛异常（含 `CancelledError`） | 凭令牌 `--cancel`；确认窗口不在才清记录，没确认就**保留**记录留给下次启动重试 | 此刻 start 的结果**未知**，可能什么都没建、也可能窗口已开而回执丢了 |
| `--claim` 失败 | 凭令牌 `--cancel`，记录同样只在确认后清理 | 落盘发生在 claim **之前**，所以此刻 journal 里已经有带 `session_id` 的记录，即便取消没被确认，v1 的 journal 兜底路径仍覆盖得住这个窗口 |

**实测确认的 `bsk session request` 语义**（本机 bsk 0.3.2，真实 daemon）：
`session request <令牌> --help` → exit 0 且输出含 `--prepare`；`--prepare` →
`{"state": "prepared", "session": null}` 且**不开窗口**；未 prepare 过的令牌查询 →
`{"state": "unknown"}`，exit 0；`--cancel` → `{"state": "closed", "cleanup_error": null}`；
对不存在的令牌 `--claim` → exit 1 + `code="not_found"`。

**代价**：每次建立新会话多两条命令（prepare + claim），实测约多花 80 毫秒，只在新会话
建立时产生一次。`enable_request_id=false` 完全回到旧行为 —— 它是排查用的回退开关。

---

## 6. 测试策略

分四层 + 专项验证。前两层不需要任何外部依赖，后两层需要真实环境。

| 层 | 范围 | AstrBot | 浏览器 | 脚本 |
|---|---|---|---|---|
| L1 单元测试 | `bsk/*` 纯逻辑：错误映射、VOM 解析、配置校验、截图魔数、会话状态机、框架超时读取与钳制、8 个主工具的 action 枚举与逐 action 参数校验 | ❌ | ❌ | `tests/test_*.py`（840 个用例，`unittest` 口径） |
| L2 契约测试 | `main.py` 能被真实 AstrBot import、16 个工具注册成功、docstring schema 正确、硬约束（无 `__del__` 等）满足 | ✅ | ❌ | `verify_astrbot_contract.py` |
| L3 服务层集成 | 真实调用 bsk：开→导航→读→截图→关，含并发与会话过期自动重建 | ✅ | ✅ | `verify_integration.py` |
| L4 工具层端到端 | 直接 await `main.py` 里的工具函数，验证权限门、参数校验、异步生成器行为、异常包装 | ✅ | ✅ | `verify_tools_e2e.py`（6 个旧工具：`bsk_open/read/act/screenshot/close/status`）、`verify_logs_tool.py`（`bsk_logs`）、`verify_evaluate_gate.py`（`bsk_evaluate` 的权限三分支）、`verify_new_tools_real.py`（8 个新工具、57 个 action） |

**为什么必须有 L4**：L2 只证明工具"注册成功"，L3 只走到服务层。工具函数内部那层
（URL 校验、权限判定、`bsk_screenshot` 的 async generator、异常是否被吞掉）
只有 L4 能覆盖 —— 而那正是最容易出 bug、且出错时用户直接看到堆栈的地方。

专项验证（各自针对一类"单元测试覆盖不到"的风险）：

| 脚本 | 针对的风险 | 需要浏览器 |
|---|---|---|
| `verify_tool_execution_chain.py` | 走 AstrBot 真实工具执行器（`call_local_llm_tool` + partial 绑定 + async generator 消费），而非直接调函数 | ✅ |
| `verify_restart_real.py` | 跨进程验证"强杀后重启自愈"，含他人会话干扰项 | ✅ |
| `verify_recover_real.py` | journal 恢复逻辑对真实 daemon 的行为（含碰撞防护） | ✅ |
| `verify_journal_safety.py` | 碰撞防护：id 相同但窗口号不同时一条 stop 都不发 | ❌ |
| `verify_browser_ambiguity.py` | 多浏览器歧义：1 个免配置 / ≥2 报错 / 已配置尊重配置 | ❌ |
| `verify_config_pipeline.py` | 配置从文件 → `AstrBotConfig` → `Settings` → 实际 bsk 命令行参数的完整贯通 | ❌ |
| `verify_config_type.py` / `verify_config_consistency.py` | 配置来源形态（dict vs `AstrBotConfig` 对象）、schema 与代码默认值一致 | ❌ |
| `verify_evaluate_gate.py` | `bsk_evaluate` 的权限门三分支（独立开关 / 强制管理员盖过 `admin_only` 与白名单 / 放行），用假 event + 假 service，不执行任何 JS | ❌ |
| `verify_install.py` | 从已提交文件导出干净副本并加载，验证"别人拿到仓库能用" | ❌ |
| `verify_discovery.py` | AstrBot 自己的插件发现函数能否找到本插件 | ❌ |
| `verify_failure_ux.py` | 环境未就绪时的提示质量（不能是 Python 堆栈） | ❌ |
| `verify_stop_timing_real.py` | `session stop` 真实耗时（为超时预算提供数据依据） | ✅ |
| `verify_wait_navigation_real.py` | 新增暴露的 `wait_for_navigation` 动作真实可用且只读 | ✅ |
| `verify_lazy_debug_real.py` | `bsk_debug` 按需加载的完整链路：默认不在工具列表、调 `bsk_load_tools` 后进入**当轮** `req.func_tool`、加载后能真的执行；以及"重新按注册表构造的请求里它仍然不在"（证明没有偷偷改全局） | ✅ |
| `verify_legacy_groups.py` | `legacy_tools` × `legacy_fringe_tools` 四种组合的真实注册数（15 / 12 / 8 / 8）、`bsk_evaluate` 始终在、`lazy_debug_tool=false` 时 `bsk_debug` 回来 | ❌ |
| `verify_release_ready.py` | 发布前自检（37 项）：元数据、合规红线、架构约束、工作区卫生 | ❌ |

logger 注入与数据落盘位置这两条改动另有专门的单元测试，见 §6.3。

运行方式（用 AstrBot 自带解释器，因为插件就跑在它上面）：

```powershell
$py = "D:\AstrBot\backend\python\python.exe"
cd <插件目录>                                 # 即本仓库根目录
& $py -m unittest discover -s tests        # L1（840 个）
& $py tests\verify_astrbot_contract.py     # L2
& $py tests\verify_integration.py          # L3（需要浏览器）
& $py tests\verify_tools_e2e.py            # L4（需要浏览器）
& $py tests\verify_release_ready.py        # 发布前自检
```

**注意**：AstrBot 自带的那个解释器里没有 `pytest`（`ModuleNotFoundError`），所以上面一律用
`unittest`；本机另有一个装了 pytest 的独立解释器（`D:\Python312\python.exe`），可以用它跑同一批
用例（`python -m pytest tests/ -q`，实测 839 passed / 1 skipped，与 `unittest` 口径的 840 个
用例一致 —— 差的 1 个是被 skip 的那条）。

L3/L4 的强制安全边界（这些脚本会真的操作浏览器）：
只访问 `example.com`；绝不借用用户标签页；不做 click/fill/press/upload/download/evaluate；
绝不使用 `session stop --all`（会误停用户的 DSH 会话）；结束时按精确 id 清理自己的会话。
断言"自己的会话没了"时，只比对自己创建的 session id，不能断言"浏览器会话数为 0"
（那会把别人的会话算进来而误报）。并且以 daemon 为事实来源：清理后再查一次
`session list`，必要时按精确 id 补刀，不要只信管理器自述"已清空"。

**L2/L4 的路径前提**：AstrBot 用 `__import__("data.plugins.<目录>.main")` 加载插件，
所以脚本需要把 `~/.astrbot` 放进 `sys.path`，且插件要真的安装在
`~/.astrbot/data/plugins/astrbot_plugin_bsk_browser/` 下。脚本已自行处理路径。

**所有 import astrbot 的测试脚本必须先 `os.environ.setdefault("ASTRBOT_ROOT", ...)`**：
AstrBot 解析数据路径时优先读该变量，否则普通模式下用当前工作目录
（`astrbot_path.py:29-35`），会在项目里生成 `data/cmd_config.json`
（AstrBot 主配置，含 provider API 密钥与管理员 QQ 号）—— 而本仓库是要公开发布的。
这个坑栽过 3 次，现已由 `verify_release_ready.py` 静态扫描（AST 解析真实 import
语句）自动拦截。

---

## 6.1 已修复的真实 bug（回归测试守护，勿回退）

记录这些是因为它们都属于"只有真实环境才暴露"的类型，改动相关代码时容易重新引入。

| # | 缺陷 | 根因 | 表现 | 守护测试 |
|---|---|---|---|---|
| 1 | stdin 自杀式取消 | `communicate()` 会在读取前关掉 stdin，而环境变量设了 `BSK_CANCEL_ON_STDIN_CLOSE=1`，bsk 把"stdin 被关"当成用户按 Ctrl-C | 随机的 `tool dispatch cancelled after extension cleanup`；并发 8 个 observe 只有 5 个成功 | `test_runner.py` + L3 的并发用例 |
| 2 | GBK 编码崩溃 | Windows 下 Python 默认用 cp936 解码，页面含阿拉伯文/俄文时抛 `UnicodeDecodeError`，且异常在 reader 线程抛出，主线程只看到 `None` | 读取任何多语言页面即崩，且报错信息毫无指向性 | `test_runner.py::TestEncoding` |
| 3 | VOM ref 前缀丢失 | 正则捕获组漏了 `e`，`@e1` 被存成 `"1"` | 传给 bsk `--ref` 的值非法，所有元素操作失效 | `test_pages.py` |
| 4 | 截图清理失效 | `cleanup_shots` 只下探一层，而文件写在 `shots/<session>/` 两层 | 清理永远返回 0，磁盘无限增长 | `test_shots.py` |
| 5 | 配置项形同虚设 | 各命令硬编码超时，忽略用户的 `command_timeout_sec` | 用户调大超时对慢页面毫无帮助 | `test_service.py` |
| 6 | 权限提示与实现相反 | `validate_settings` 的文案说白名单"不生效"，实际是白名单优先 | 用户按提示操作得到相反结果 | `test_config.py` |
| 7 | 多浏览器时静默随机选 | `probe_browser` 只在恰好 1 个时自动选，多个时返回空 → 交 bsk 自选；且 README 已承诺"会报错"但代码没做 | 用户连了 2 个浏览器时"有时候对有时候不对"，无从排查 | `verify_browser_ambiguity.py` |
| 8 | 测试自身泄漏会话 | 某用例把假 id `"zzzz"` 写进 args builder，导致懒创建的真实会话在重建时被遗弃 | 全量回归后 daemon 残留会话 | `verify_integration.py` 的差集断言 + 兜底强清 |
| 9 | README 与实现方向相反 | `session_scope:"user"` 说"跨群共用"，实际键含 umo → 跨群独立 | 用户按文档理解会误判资源占用 | README 核对报告 |
| 10 | 启动超时留下永久孤儿浏览器窗口 | 顺序是「先 `await self._start()`、成功后再 `_journal_add()`」；start 超时直接抛异常，落盘那一步**永远执行不到** | 窗口已经开出来但插件不知道它的 id，journal 里也没有它，`recover_orphans()` 够不着 → 桌面留下关不掉也清不掉的窗口，重启 AstrBot 同样带不走 | `test_session.py` 的可恢复启动用例 + `verify_recover_real.py`；机制见 §5 D12 |

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
| **全页截图耗时（关键数据，文档多处引用）** | 本机实测：短页面 `example.com` 视口截图 0.12s、全页截图 2.91s；长页面 Wikipedia 条目全页截图 11.72 / 11.11 / 10.91s（1820x11741，4.5MB） | ① 整页截图默认预算取 120s（约 10 倍余量），不是拍脑袋的 180s；② **"必须两处一起调大才能用整页截图"是错误说法** —— 实测只用了框架 120s 上限的约 1/10，默认配置下什么都不用改（该说法曾写在 README/ARCHITECTURE 里，已删除）；③ 真正要做的是钳制，别被框架从外面掐断（见 §5 D6） |
| **框架超时是"每一步"的，不是"整个工具"的** | `astr_agent_tool_exec.py:691` 是 `await asyncio.wait_for(anext(wrapper), timeout=tool_call_timeout)` —— 包在每一次 `anext` 上 | async generator 若中途 yield，计时器会重置。`bsk_screenshot` 正是先 yield 图片再 yield 文本；但不要依赖这一点去绕过超时 —— 钳制仍是必需的，因为它同时保证了"超时由插件报中文"这件事 |

### 6.3 logger 注入与数据落盘的守护测试

这两项改动各有一个专门的测试文件。它们存在的理由都是"**表面行为相同、做错了看不出来**"：

| 文件 | 针对的风险 | 关键断言 |
|---|---|---|
| `test_logger_injection.py` | "注入"与"被吞掉"在行为上无法区分 —— 若某处漏改、仍在调空实现，`NullLogger` 与真 logger 的表现完全一样 | 记录型假 logger 上**必须出现记录**（`test_injected_logger_actually_receives_records`）；未注入时全分支不抛异常；`bsk/` 零 `import logging`、零 `logging.getLogger`、零 `import astrbot`，且 `main.py` 亦无内置 logging（`ast` 扫描，非关键字搜索） |
| `test_data_dir.py` | 落盘位置的断言极易写成"在临时目录里"——而测试自己就把 data_dir 造在临时目录下，那样写永远是绿的 | 降级目标钉到 `<系统临时目录>/astrbot_bsk_browser` 这一层；用假 runner 记录的 `--out` 实参验证截图真实落点；显式配置必须压过 `data_dir` |

`test_journal.py::TestDefaultPath` 已按新语义改写：注入 `data_dir` 时落在其下，
未注入时退回临时目录 —— **降级分支保留断言**，因为它是审核明确要求保留的行为。

---

## 7. 版本控制

- 仓库根 = 本仓库根目录（插件目录本身即仓库根，便于直接 clone 进 `data/plugins/`）
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
│   ├── logger.py            #   日志接口 + NullLogger 兜底（零依赖）
│   ├── paths.py             #   插件数据目录解析与降级（零依赖）
│   ├── models.py
│   ├── errors.py
│   ├── config.py
│   ├── runner.py
│   ├── session.py
│   ├── pages.py
│   ├── shots.py
│   ├── journal.py            #   会话所有权 journal（原子写 + 损坏容错）
│   ├── tools.py              #   8 个主工具的规格唯一事实源（schema + 归一化 + 校验）
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

**运行期数据**（不进版本库，位于 AstrBot 的数据目录下）：

```
data/plugin_data/astrbot_plugin_bsk_browser/
├── sessions.json            # 会话所有权 journal（跨重启持久化）
└── shots/                   # 截图（按会话分子目录，自动清理）
```
