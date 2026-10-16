"""config.toml 加载。

搜寻顺序：./config.toml -> ~/.config/xbm/config.toml
敏感信息（cookie）绝不进 config.toml，走 `xbm auth login`。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class NotifyConfig:
    bark_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


@dataclass
class ProxyConfig:
    url: str = ""  # 空表示直连，如 http://127.0.0.1:7890


@dataclass
class DownloadConfig:
    dir: str = ""  # 留空 → {data.root}/media；可指向独立卷
    max_retries: int = 5
    workers: int = 2


@dataclass
class Config:
    data_root: Path
    page_size: int = 20
    max_pages: int = 10
    request_delay: float = 1.5
    fail_alert_threshold: int = 3
    gallery_dl_extra_args: list = field(default_factory=list)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)

    @property
    def state_dir(self) -> Path:
        """cookies.json / cookies.txt / xbm.db 存放目录。"""
        return self.data_root / ".xbm"

    @property
    def media_dir(self) -> Path:
        """媒体文件根目录：[download] dir 优先，否则 {data.root}/media。"""
        if self.download.dir:
            return Path(self.download.dir).expanduser()
        return self.data_root / "media"


def _candidates(explicit: Path | None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    return [
        Path.cwd() / "config.toml",
        Path.home() / ".config" / "xbm" / "config.toml",
    ]


def load(path: Path | None = None) -> Config:
    data: dict = {}
    for cand in _candidates(path):
        if cand.is_file():
            with open(cand, "rb") as f:
                data = tomllib.load(f)
            break

    d = data.get("data", {})
    s = data.get("sync", {})
    g = data.get("gallery_dl", {})
    n = data.get("notify", {})
    px = data.get("proxy", {})
    dl = data.get("download", {})

    root = d.get("root") or str(Path.home() / "x-bookmarks")
    return Config(
        data_root=Path(root).expanduser(),
        page_size=int(s.get("page_size", 20)),
        max_pages=int(s.get("max_pages", 10)),
        request_delay=float(s.get("request_delay", 1.5)),
        fail_alert_threshold=int(s.get("fail_alert_threshold", 3)),
        gallery_dl_extra_args=list(g.get("extra_args", [])),
        notify=NotifyConfig(
            bark_url=n.get("bark_url", ""),
            telegram_bot_token=n.get("telegram_bot_token", ""),
            telegram_chat_id=n.get("telegram_chat_id", ""),
        ),
        proxy=ProxyConfig(url=px.get("url", "") or ""),
        download=DownloadConfig(
            dir=dl.get("dir", "") or "",
            max_retries=int(dl.get("max_retries", 5)),
            workers=int(dl.get("workers", 2)),
        ),
    )
