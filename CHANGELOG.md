# 变更日志

本文件记录本项目的所有重要变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增
- 项目架构设计（`ARCHITECTURE.md`），含 9 条基于 AstrBot 4.28.1 源码实证的硬约束
- `bsk/errors.py`：bsk 退出码（6 档）与错误码分类，含"结果未知禁止重试"安全语义
- `bsk/models.py`：领域模型，全部容错解析（覆盖 `entries` 字段消失等实测坑）
- `bsk/runner.py`：子进程调用层，集中处理 5 个实测坑：
  - 列表参数不过 shell（无命令注入面）
  - `communicate()` 包裹超时（Windows 管道句柄可能永不返回）
  - 三级降级取消 + `BSK_CANCEL_ON_STDIN_CLOSE`（Windows 优雅取消必需）
  - 显式 UTF-8 解码（GBK 环境下多语言页面会崩溃）
  - 成败判定只看退出码（错误 JSON 在 stdout，clap 错误在 stderr）
- 双许可（`MIT OR AGPL-3.0-or-later`）与第三方组件声明
- `tests/test_runner.py`：41 个单元测试，用假可执行文件覆盖全部错误路径

[未发布]: https://github.com/yourname/astrbot_plugin_bsk_browser/commits/main
