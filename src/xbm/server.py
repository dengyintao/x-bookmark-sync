"""Web 服务（fpk 常驻）+ REST API。

形态：飞牛 fpk 原生应用，/app/xbm 网关 + Unix Socket，不暴露 TCP 端口。
复用 meizang 的轻量 HTTP 服务风格（stdlib 或 Flask 二选一）。

API 草案（见 DESIGN.md §9.3）：
    GET  /api/status
    POST /api/auth/login        # {username, password, totp?}，密码只进内存
    GET  /api/auth/status
    POST /api/sync/run
    POST /api/sync/backfill     # {max_pages}
    GET  /api/sync/runs
    GET  /api/sync/log?run_id=
    GET  /api/bookmarks         # ?q=&author=&page=
    GET  /api/bookmarks/:id
    GET/PUT /api/settings

同步引擎以前台后台线程方式运行，每小时间隔触发 fetcher/downloader/articles，
与 Web 服务共享同一 SQLite。

鉴权：MVP 依赖飞牛桌面登录（内网使用）；对外暴露时再移植 meizang 登录体系。

TODO(M4): 服务骨架 + API 实现 + 前端页面（总览/认证/同步/画廊/设置）。
"""
from __future__ import annotations


def run() -> None:
    raise NotImplementedError("TODO(M4)")
