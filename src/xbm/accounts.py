"""Separate SQLite libraries and media roots for each logged-in X handle."""
from __future__ import annotations

import re
import threading
from dataclasses import replace

from .config import Config
from .db import Store
from .downloader import Downloader


def account_key(value: object) -> str:
    if value is None or value == '':
        return ''  # Pre-account clients and queued data remain explicitly unassigned.
    if not isinstance(value, str) or not re.fullmatch(r'x:[A-Za-z0-9_]{1,15}', value):
        raise ValueError('invalid X account identifier')
    return value.lower()


class AccountManager:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.RLock()
        self._libraries = {'': (config, Store(config.state_dir / 'xbm.db'))}
        self._downloaders = {}
        self._running = False
        root = config.data_root / 'accounts'
        for path in sorted(root.glob('*/.xbm/xbm.db')):
            handle = path.parent.parent.name
            if re.fullmatch(r'[a-z0-9_]{1,15}', handle):
                self.get('x:' + handle, create=True)

    def get(self, value: object = '', *, create: bool = False) -> tuple[Config, Store]:
        key = account_key(value)
        with self._lock:
            if key not in self._libraries:
                if not create:
                    raise KeyError('account not found')
                handle = key[2:]
                root = self.config.data_root / 'accounts' / handle
                media = self.config.media_dir / 'accounts' / handle
                if not root.resolve().is_relative_to(self.config.data_root.resolve()) or not media.resolve().is_relative_to(self.config.media_dir.resolve()):
                    raise ValueError('account directory outside data root')
                cfg = replace(self.config, data_root=root,
                              download=replace(self.config.download, dir=str(media)))
                store = Store(cfg.state_dir / 'xbm.db')
                self._libraries[key] = cfg, store
                if self._running:
                    self._start_downloader(key, cfg, store)
            return self._libraries[key]

    def list(self) -> list[dict]:
        with self._lock:
            return [{'id': key, 'label': '@' + key[2:] if key else '未归属（旧版本数据）',
                     'tweets': store.count_tweets()}
                    for key, (_, store) in self._libraries.items()]

    def _start_downloader(self, key: str, cfg: Config, store: Store) -> None:
        downloader = Downloader(cfg, store)
        downloader.start()
        self._downloaders[key] = downloader

    def start(self) -> None:
        with self._lock:
            self._running = True
            for key, (cfg, store) in self._libraries.items():
                self._start_downloader(key, cfg, store)

    def close(self) -> None:
        with self._lock:
            self._running = False
            for downloader in self._downloaders.values():
                downloader.stop()
            for _, store in self._libraries.values():
                store.close()
