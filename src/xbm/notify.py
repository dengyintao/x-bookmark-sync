"""失败告警。

- 连续失败 >= fail_alert_threshold 次才告警（防抖）。
- 通道：Bark / Telegram Bot / 仅日志（按 config.notify 配置）。
- 告警内容：最近一次错误 + sync_runs 统计，不带 cookie 等敏感信息。

TODO(M4)
"""
from __future__ import annotations


def alert(message: str) -> None:
    raise NotImplementedError("TODO(M4)")
