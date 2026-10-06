# 变更日志

本文件记录本项目的所有重要变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.3.0] - 2026-10-07

对齐腾讯 BrowserSkill 的 DSH 插件形态：把工具按能力拆成 8 个多动作主工具（合计 57 个 action），
把最大的那个改成按需加载，给旧工具分了两级开关，并补上可恢复启动。

这次发布**不是纯向后兼容**：旧工具默认只保留常用的 4 个，`bsk_inspect` 的调试 action 也换成了
独立工具 —— 两处破坏性变更见下方"变更"一节，升级前请先看那两条。

本次移植过程中用真机实测逐条核对了参考实现的行为，因此下方"修复"一节的多数条目
不是新代码自带的缺陷，而是**照抄推理、没有实测**留下的错误 —— 单测全绿时它们依然存在。

### 新增

- **8 个多动作主工具（共 57 个 action）**，参数名与 action 名均与参考实现一致：
  - `bsk_session`（3）：打开 / 关闭 / 列出机器人自己的浏览器会话
  - `bsk_page`（5）：导航、前进、后退、刷新（可绕缓存）、等待页面生命周期
  - `bsk_inspect`（6）：读取页面（`observe` / `snapshot` / `html`）、`screenshot`、
    控制台与网络请求
  - `bsk_debug`（24）：抓包与网络调试 —— 性能、请求证据、导出，以及显式控制网络流量
  - `bsk_load_tools`（1）：按需加载大工具，目前只有 `debug` 一组
  - `bsk_interact`（9）：点击、悬停、滚轮、滚动到元素、聚焦、失焦、填表、下拉选择、按键
  - `bsk_tabs`（6）：列出 / 新建 / 切换 / 关闭 / 借用 / 归还标签页
  - `bsk_assist`（3）：窗口缩放、设备模拟、在页面上向真人求助
- **`bsk_debug` 按需加载（新配置项 `lazy_debug_tool`，默认 `true`）**：这个工具光 schema 就
  4431 字符，而绝大多数对话并不调试网络，所以默认**不**把它注册给模型。模型需要调试时先调
  常驻的小工具 `bsk_load_tools`，插件再把 `bsk_debug` 加进**当前这一轮请求**的工具集 ——
  同一轮的下一次 LLM 请求就带上它了，而用户的下一条消息会重新回到未加载状态。实测每轮省下
  4937 / 4939 / 4971 字符（Google / Anthropic / OpenAI 三种 provider 格式）
- **可恢复启动（新配置项 `enable_request_id`，默认 `true`）**：建立会话时先用一次性令牌
  「写前落盘 + 向 daemon 预约」，再带令牌启动、成功后认领。启动回执超时或丢失时，仍能凭令牌
  找回并关掉那个已经开出来的窗口（根因与修法见"修复"一节）。代价是每次建新会话多两条命令，
  实测约多花 80 毫秒，且只在新会话建立时产生一次
- **`legacy_fringe_tools`（默认 `false`）**：旧工具从"全开或全关"改成两级 —— 常用的 4 个
  （`bsk_screenshot` / `bsk_close` / `bsk_status` / `bsk_logs`）跟着 `legacy_tools` 常驻；
  罕见的 3 个（`bsk_open` / `bsk_read` / `bsk_act`，都有等价新工具）由这一项单独控制。
  实测少注册这 3 个工具省下 1941 / 2013 / 2061 字符（三种 provider 格式）
- **标签页管理**：`bsk_tabs` 支持列出、新建、切换、关闭，以及 **借用用户自己已打开的
  标签页**（`borrow`，用完 `return` 归还）—— 需要登录态的页面不必再让用户手动重开一次
- **设备模拟与窗口缩放**：`bsk_assist` 的 `emulate` 内置 7 种设备预设
  （iPhone / Pixel / Galaxy / iPad 等），`resize` 可直接改窗口尺寸，另可单独清除模拟
- **网络调试**：`bsk_debug` 提供 24 个 action —— 开始 / 结束抓包、
  读取页面与请求证据、查看规则、导出，以及**显式控制网络流量**：`rule_add` /
  `rule_enable` 可拦截、改写或伪造真实请求，`replay` 会重新发送一次请求
  （**可能改动服务端数据**）
- **页面内求助真人**：`bsk_assist(action="request-help")` 让模型在页面上高亮目标并
  说明要做什么，然后等用户完成（例如扫码、短信验证码）。带 `completion_criteria`
  完成条件判定：`continued` / `completed` 才算完成，`cancelled` / `timed_out` /
  `navigated` / `disabled` 都不是
- **观察的游标分页**：`observe` 支持 `cursor` / `max_depth` / `max_tokens`，
  返回里给出 `next_cursor` 供续读，内容被截断时模型不再无从下手
- **新配置项 `legacy_tools`（默认 `true`）**：控制旧的 8 个工具是否继续注册。
  默认开启以保证升级无感；关掉可省下每轮对话 1631 / 1739 / 1803 字符的工具描述开销
  （实测，三种 provider 格式；默认状态下 fringe 那 3 个本来就没注册，所以这里的数字
  比"8 个旧工具全开与全关之差"小）。
  `bsk_evaluate` 不受这一项影响（它有自己的高风险开关 `enable_evaluate`），
  因此关掉后停用的是 8 个中的 7 个。该项只在插件重新加载时生效

### 修复

按严重性排序。第一条是实际使用中暴露的缺陷，其余为本次移植中实测发现：

- **启动超时留下永久孤儿浏览器窗口（P0）**：此前的顺序是「先等 `session start` 返回，
  成功之后再写会话记录」。一旦 start 超时（或回执丢失），写记录那一步**永远执行不到** ——
  窗口可能已经开出来了，但插件拿不到它的 session id、记录里也没有它，`recover_orphans()`
  同样够不着，于是桌面上留下一个既关不掉也清理不掉的窗口，重启 AstrBot 也带不走。
  现改为**写前落盘**：发 start 之前先把一次性令牌写进 journal，凭令牌向 daemon 预约启动，
  成功后认领；之后无论 start 那一步发生什么（超时、断连、进程被杀），都能凭令牌把那次
  启动造出来的会话找回来关掉。落盘失败时不带令牌走普通启动（退回旧行为），而不是拒绝启动
- **`scroll-to` 完全不可用（P0）**：`bsk_interact` 的 action 名是连字符
  （`scroll-to`，与 CLI 子命令一致），而内部规格表的键写成了下划线 `scroll_to`，
  两边对不上 —— 模型传对了名字反而报错。现两种写法都收，输出统一为连字符
- **`tab_id` 在 8/10 个 action 被静默丢弃（P0）**：校验阶段读了它、却没写回结果，
  模型指定了标签页**不报错也不生效**，命令打在另一个标签上。现全部保留
- **`completion_criteria` 三层键名不一致，`request-help` 完全失效（P0）**：
  schema 用 camelCase、内部校验用 snake_case、送进 CLI 的又是另一套，
  三层各写各的。现在有唯一的转换表，两种写法都收
- **`debug` 的 `replay` / `rule_*` 在"上次结果不确定"时仍会执行（P0，安全分级错误）**：
  这两类动作会改变外部可见状态，判断标准应是"重放一次会不会改变服务端数据"，
  而不是"bsk 自己会不会校验"。现已归入写类动作，在状态不确定时一律挡住
- **`debug_action=wait` 的超时倒挂（P0）**：`wait_ms` 的合法上限是 60 秒，
  而外层超时默认也是 60 秒 —— 一次**合法的最长等待**会被自己的超时掐死，
  用户看到超时错误而不是等到的结果。现保证外层超时严格大于 bsk 自身预算
- **`observe` 的 `cursor` / `max_depth` / `max_tokens` / `tab_id` 被丢弃（P0）**：
  参数收下了却没有传下去，分页与限深限长形同虚设
- **`console` 时间戳让渲染整条崩掉（P0）**：`bsk` 的 `console` 用 Unix 毫秒、
  `network` 用相对毫秒，此前按同一种解释直接 `localtime()`，`console` 抛
  `OSError` 导致渲染失败。现按来源分别解释
- **截图落点出现重复的 `shots` 层级（P1）**：数据目录解析返回 `.../shots`
  而落盘函数内部又拼一层，实际写成 `.../shots/shots/<会话id>/`
- **若干文档事实错误（P1）**：工具数、用例数、自检项数均与实际不符；
  「视口截图超时固定 30 秒」也是错的（实测随 `command_timeout_sec` 变化，
  30 秒只是下限）。契约测试的工具清单改为从源码自动推导，杜绝再次过时

### 变更

**以下两条是破坏性变更** —— 升级后老用户能直接感知到，请先确认自己不受影响：

- **⚠️ `bsk_open` / `bsk_read` / `bsk_act` 升级后不再注册**：旧工具从"全开或全关"改成两级，
  新配置项 `legacy_fringe_tools` 默认 `false`，这三个有等价新工具的名字默认停用
  （`bsk_open` → `bsk_page` + `bsk_inspect`，`bsk_read` → `bsk_inspect`，`bsk_act` → `bsk_interact`）。
  提示词、工作流或使用习惯里还在直接写这三个名字的，请打开 `legacy_fringe_tools` 找回它们，
  或改用新工具。常用的 4 个旧工具（`bsk_screenshot` / `bsk_close` / `bsk_status` / `bsk_logs`）
  不受影响，`legacy_tools` 的默认值仍是 `true`
- **⚠️ `bsk_inspect(action="debug")` 不再可用**：24 个调试 action 已拆成独立工具 `bsk_debug`，
  `bsk_inspect` 只保留 6 个读页面类 action。原来写 `bsk_inspect(action="debug", debug_action=...)`
  的提示词需要改成 `bsk_debug(action=...)`（参数名也去掉了 `debug_` 前缀）
- **`bsk_inspect` 的调试参数拆到新的 `bsk_debug`**：它的 schema 从 5436 字符降到 1407 字符
  （36 个参数降到 12 个），不调试的对话不必再为那 23 个调试参数付费
- **工具块整体瘦身**：默认注册的工具数 14 → 12（8 个主工具里 `bsk_debug` 按需加载、
  8 个旧工具里 fringe 3 个默认停用），本插件占用的工具说明从 18914 / 19097 / 19417 字符
  降到 13300 / 13413 / 13717 字符（Google / Anthropic / OpenAI 三种 provider 格式），
  **每轮少 5614 / 5684 / 5700 字符（约 29%）**
- **`bsk_session` 不接受模型传 `request_id`**：`request_id` 的语义已实测确认
  （`bsk session request` 的 prepare 预约 / claim 认领 / cancel 关闭 / 未预约令牌返回
  `unknown`），但它**不作为参数暴露给模型** —— 可恢复启动改由插件自己用配置项
  `enable_request_id`（默认开启）驱动，令牌由插件生成与落盘。详见下方「与 BrowserSkill 的差异」
- **旧工具的 docstring 增加引导语**（"兼容保留，新用法请优先用 X"），
  让模型在旧写法与新工具之间优先选择新工具。行为一个字未改。
  8 个旧工具里有 6 个加上了引导语；`bsk_status` 与 `bsk_evaluate` 刻意不加 ——
  前者给出的诊断信息是新工具看不到的，后者是本插件独有能力，
  两者都没有等价替代品，加引导语反而会把模型从唯一可用的工具上引开

### 与 BrowserSkill 的差异

以下差异是**有意为之**，不是未完成项，如实标注如下：

- **`request_id` 由插件驱动，而不是交给模型**：可恢复启动能力**已支持**（`enable_request_id`，
  默认开启），但接口形态不同 —— 参考实现把它做成 `browser_session` 的一个可选参数，
  本插件改为插件内部生成令牌、写前落盘、启动后认领，模型侧看不到这个参数。
  这样做是因为令牌一旦由模型填，恢复路径的正确性就取决于模型是否填对值，
  而它只在启动失败这条罕见路径上起作用，暴露给模型只会增加误用的机会
- **`current` 会话在 stop 后不回退**：参考实现会回退到最近 active 的会话，
  本插件不回退 —— 避免用户的下一步操作被静默作用在另一个会话上
- **无 SSE 实时观察 / 侧边栏 / 缩略图**：这三项属于展示层，AstrBot 没有对应接缝
- **新增的 `bsk_inspect(action="screenshot")` 只回文字、不发图片**：它的 handler
  返回字符串而不是 async generator，拿不到发图那条路。**要发图或要整页截图，
  请继续用 `bsk_screenshot`** —— 这两件事目前只有它做得到
- **懒加载的触发方式不同**：参考实现靠 skill 触发注册，本插件改由模型显式调用常驻的
  小工具 `bsk_load_tools` 触发；改的是**当轮** `ProviderRequest.func_tool`，
  同一轮的下一次请求就生效，不需要等下一步操作
- **`bsk_evaluate`（执行任意 JS）是本插件独有的**：DSH 不暴露它，因此新工具里
  没有对应替代。风险由独立开关（`enable_evaluate`）与强制管理员双重门控

## [0.2.0] - 2026-10-06

对齐腾讯 BrowserSkill 的 DSH 插件形态：新增 6 个多动作主工具（合计 33 个 action），
旧版 8 个单动作工具全部保留、由 `legacy_tools` 开关控制。

这一版是**首个进插件市场的版本**（0.1.1 只修了审核拒绝项，未提交市场）。
当时的形态是「6 个主工具 + 8 个旧工具」，`bsk_inspect` 自带 24 个调试子动作，
工具块约 18914 字符/轮。

### 新增

- **6 个多动作主工具（共 33 个 action）**，参数名与 action 名均与参考实现一致：
  `bsk_session`（3）、`bsk_page`（5）、`bsk_inspect`（7，含 debug 的 24 个子动作）、
  `bsk_interact`（9）、`bsk_tabs`（6）、`bsk_assist`（3）
- **`legacy_tools` 配置项（默认 `true`）**：控制是否同时注册旧版 8 个工具
- **架构图** `docs/architecture.{html,png,json}`，并在 README 顶部嵌入

### 修复

本版移植过程中用真机实测逐条核对了参考实现的行为，因此下面的多数条目不是新代码
自带的缺陷，而是**照抄推理、没有实测**留下的错误 —— 单测全绿时它们依然存在：

- **`scroll-to` 完全不可用**：action 键名在下划线与连字符之间不一致，掉进了错误分支
- **`tab_id` 在 8/10 个 action 上被静默丢弃**：模型指定了标签页，命令却打在另一个标签上
- **`completion_criteria` 三层键名不一致**，导致 `request-help` 完全失效
- **`debug` 的 `replay` / `rule_*` 在"上次结果不确定"时仍会执行**（安全分级错误）
- **`debug_action=wait` 超时倒挂**：合法的 60 秒等待会被自己的 60 秒外层超时掐死
- **`observe` 的 `cursor` / `max_depth` / `max_tokens` / `tab_id` 被丢弃**
- **`console` 的 Unix 毫秒时间戳让渲染抛 `OSError`**
- **截图落点出现重复的 `shots` 层级**
- 若干文档事实错误（工具数、用例数、自检项数）

### 变更

- `bsk_session` 的 `request_id` 参数在 0.1.x 曾被移除（不支持的能力不暴露），
  本版仍未加回 —— 它在 0.3.0 里以另一种形态落地（见该版"新增"）

## [0.1.1] - 2026-10-05

修复插件市场 LLM Guard 审核拒绝的两项问题。本次发布不含功能变更。

### 修复

- **日志改用依赖注入，不再使用 Python 内置 logging 模块**：审核规则要求 logger 必须
  来自 `astrbot.api`（`from astrbot.api import logger`），严禁使用内置 `logging` 模块；
  而本仓库另有一条被静态测试强制的分层约束 —— `bsk/` 包不得依赖 AstrBot，否则就无法
  脱离框架独立单测。两条要求正面冲突，解法是审核原文明确许可的第三条路：
  `main.py`（唯一允许依赖框架的文件）从 `astrbot.api` 取 logger 后**注入**给 `bsk/`
  各组件（`BskService` → `SessionManager` / `SessionJournal`），`bsk/` 侧只在新增的
  `bsk/logger.py` 里声明接口（`LoggerLike` 协议）并提供 `NullLogger` 兜底。
  `bsk/` 下因此既没有 `import astrbot`，也没有 `import logging`。未注入时
  （单元测试、独立脚本）走 `NullLogger`：不产生任何输出，也永不抛异常

### 变更

- **会话所有权 journal 与截图迁移到插件数据目录**：此前两者都写在系统临时目录
  （`<临时目录>\astrbot_bsk_browser\sessions.json` 与 `<临时目录>\astrbot_bsk_shots`）。
  按审核规则，跨重启持久化的数据应存放在 `data/plugin_data/astrbot_plugin_bsk_browser`
  下。现由新增的 `bsk/paths.py` 统一解析落盘位置，journal 与截图默认都落在该目录。
  **保留降级容错**（审核明确要求）：数据目录拿不到或不可写时自动退回临时目录并记一条
  警告，任何一步都不抛异常 —— 它在插件加载路径上，抛异常等于插件加载失败。
  用户显式配置的 `screenshot_dir` / `journal_path` 优先级不变，仍然最高
- 更换插件图标（1:1，512×512 PNG）

## [0.1.0] - 2026-10-03

首个版本。把本机 `bsk` 命令行工具（腾讯 BrowserSkill）包装成 AstrBot 的
LLM 可调用工具，让机器人能操作**用户已登录的真实浏览器**。

### 新增

- **6 个 LLM 工具**：`bsk_open`（打开网页）、`bsk_read`（读取页面）、
  `bsk_act`（点击/输入/按键/滚动等 13 种动作）、`bsk_screenshot`（截图并
  直接发给用户）、`bsk_close`（关闭会话）、`bsk_status`（环境诊断）
- **`bsk_evaluate`（执行任意 JavaScript，高风险，默认关闭）**：第 7 个工具，
  在页面里执行 JS 表达式并返回结果。**默认关闭**（`enable_evaluate=false`）
  且**强制仅管理员**（`evaluate_require_admin=true`，优先于 `admin_only`
  与用户白名单）。独立成工具而非 `bsk_act` 的一个动作，因此可以被单独禁用、
  也能被 AstrBot 原生的 `tool_permissions` 单独控制。实测确认：JS 抛异常时
  bsk 退出码仍为 0，故成败以返回 JSON 的 `ok` 字段为准
- **`bsk_logs`（读取控制台消息与网络请求）**：第 8 个工具。`bsk/service.py`
  里的 `read_console` / `read_network` / `render_console` 此前已实现但没有
  调用点，模型拿不到这两个能力。参数 `kind`（`console` / `network`，默认
  `console`）与 `since`（增量游标，`--since N` 是开区间，返回里给出下次该传
  的值）。只读，走 `_denied` 权限门。空结果返回明确中文说明而不是空串；
  复用 `render_console` 的 URL 截断，防止 `network` 把内联的
  `data:image/png;base64,...` 塞进模型上下文
- **15 项可配置项**，默认**仅 AstrBot 管理员可用**（`admin_only`），
  支持用户白名单与「一个群一条 / 每人一条」两种会话隔离粒度。
  其中 `fullpage_timeout_sec` 专门控制整页截图的等待上限（默认 120 秒，
  可调 30–600），不再与其余命令共用 `command_timeout_sec`
- **分层架构**：框架耦合只出现在 `main.py`，业务逻辑全在 `bsk/` 包内
  且不依赖 AstrBot，因此可脱离框架独立单元测试
- **会话 journal**：持久化记录自己创建的浏览器会话，插件被强杀后重启时
  能清理遗留会话，且通过比对 `agent_window_id` 避免误停其他程序的会话
- `session stop` 对瞬时故障有界重试，降低会话泄漏概率
- 双许可 `MIT OR AGPL-3.0-or-later` + 第三方组件声明

### 安全性

- 默认仅管理员可用；关闭时启动即打印安全警告
- 拒绝 `file://` 等非 http(s) 协议，模型无法借此读取本机文件
- `evaluate`（执行任意 JS）**默认不开放**：需要用户显式打开 `enable_evaluate`，
  且默认强制仅管理员（`evaluate_require_admin` 优先于 `admin_only` 与白名单），
  开启任一开关时插件启动都会打印针对性的安全提醒
- 全程不使用 `bsk session stop --all`，只按精确 id 停止自己的会话

### 修复（开发期发现，均为"只有真实环境才暴露"的类型）

- **stdin 自杀式取消**：`communicate()` 会立刻关闭 stdin，而环境变量设了
  `BSK_CANCEL_ON_STDIN_CLOSE=1`，导致 bsk 把「stdin 被关」当成用户按了
  Ctrl-C，命令在执行途中被自己取消（并发 8 个请求只有 5 个成功）。
  改为保持 stdin 打开、自行并发抽取两个管道
- **GBK 编码崩溃**：Windows 下 Python 默认用 cp936 解码，页面含阿拉伯文/
  俄文时抛 `UnicodeDecodeError`，且异常在 reader 线程抛出、主线程只看到
  `None`，极难定位。改为显式 UTF-8 + replace
- **`session stop` 不重试导致会话泄漏**：实测在有 navigate 失败过的场景下
  48 轮泄漏 6 轮（约 12.5%），泄漏的浏览器窗口会一直留着。改为有界重试
- **配置项形同虚设**：各命令硬编码超时、忽略用户的 `command_timeout_sec`。
  改为 `max(命令内置下限, 用户配置)` 的单一规则
- **文档夸大了整页截图的超时要求（实测推翻）**：README / ARCHITECTURE 曾写
  「整页截图必须同时调大插件的 `command_timeout_sec` 与 AstrBot 的
  `tool_call_timeout`，否则会被框架掐断」。实测长页面（Wikipedia 长条目）
  整页截图为 **11.72 / 11.11 / 10.91 秒**（1820x11741、4.5MB），短页面 2.91 秒，
  而框架默认上限 120 秒 —— 只用了约 1/10，留了 108 秒余量。该说法是照搬推理、
  没有实测造成的，已删除并改为基于实测的表述
- **可能被框架"悄悄掐断"（真问题，插件侧已修）**：整页截图的超时预算与框架默认
  上限同为 120 秒时，一旦超出，用户看到的是框架抛的英文
  `tool <name> execution timeout after 120 seconds.`，而不是插件写的中文提示，
  且「会话可能留下未完成状态」被掩盖。现改为读取框架的
  `agent_runner.config.misc.tool_call_timeout` 并把插件超时钳到
  「框架上限 − 5 秒」之下（读不到时不钳制，行为与改动前一致），
  启动时打印一条解释性告警（不是报错）
- **VOM 元素引用前缀丢失**：正则漏捕获 `e`，`@e1` 被存成 `"1"`，
  导致所有元素操作失效
- **截图清理失效**：清理函数只下探一层，而文件写在两层目录下，
  清理永远返回 0
- **权限提示与实现相反**：配置校验说白名单「不生效」，实际是白名单优先

[0.3.0]: https://github.com/JosephTian876/astrbot_plugin_bsk_browser/releases/tag/v0.3.0
[0.2.0]: https://github.com/JosephTian876/astrbot_plugin_bsk_browser/releases/tag/v0.2.0
[0.1.1]: https://github.com/JosephTian876/astrbot_plugin_bsk_browser/releases/tag/v0.1.1
[0.1.0]: https://github.com/JosephTian876/astrbot_plugin_bsk_browser/releases/tag/v0.1.0

