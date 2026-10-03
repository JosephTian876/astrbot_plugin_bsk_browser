"""bsk 纯逻辑层。

这个包**故意不 import astrbot**，因此可以脱离 AstrBot 框架独立运行和单元测试。

分层：
    models.py   数据模型（纯数据）
    errors.py   错误分类（退出码/错误码 → 中文提示）
    config.py   配置解析与校验
    runner.py   子进程调用（唯一与 bsk 进程打交道的地方）
    session.py  会话生命周期管理
    pages.py    VOM 页面文本解析
    shots.py    截图文件管理
    service.py  业务编排（main.py 直接调用它）
"""

from __future__ import annotations

__all__ = [
    "config",
    "errors",
    "models",
    "pages",
    "runner",
    "service",
    "session",
    "shots",
]
