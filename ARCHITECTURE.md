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

### D6：超时分层，外层必须大于内层

| 命令 | 建议超时 |
|---|---|
| status/browsers/session list/console/network | 5s |
| observe | 15s |
| session start | 30s |
| navigate | 45s（必须 > bsk 自身默认 30s） |
| screenshot 视口 | 30s |
| screenshot --full-page | 180s |

且必须**小于 AstrBot 工具调用上限 120s**，所以默认 `command_timeout_sec` 取 60，长截图场景需用户自行调大并知晓上限。

---

## 6. 测试策略

**分三层，缺一不可**：

| 层 | 范围 | 是否需要 AstrBot | 是否需要浏览器 |
|---|---|---|---|
| L1 单元测试 | `bsk/*` 纯逻辑：错误映射、VOM 解析、配置校验、截图魔数、会话状态机 | ❌ | ❌ |
| L2 契约测试 | `main.py` 能被真实 AstrBot import、6 个工具成功注册、docstring schema 正确 | ✅ | ❌ |
| L3 集成测试 | 真实调用 bsk：开→导航→读→截图→关，含并发与超时用例 | ✅ | ✅ |

L1 用 `pytest`，对 `runner` 用假的可执行文件（stub script）模拟各类退出码与编码，**不依赖真实 bsk**。
L2 用 `D:\AstrBot\backend\python\python.exe` + `PYTHONPATH=D:\AstrBot\backend\app`。
L3 需用户授权，且**必须**：只访问 `example.com`、不借用用户标签页、不用 `--all`、结束显式清理。

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
