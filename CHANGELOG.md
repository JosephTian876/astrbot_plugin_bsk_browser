# 变更日志

本文件记录本项目的所有重要变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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

[0.1.0]: https://github.com/JosephTian876/astrbot_plugin_bsk_browser/releases/tag/v0.1.0

