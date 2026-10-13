"""Web 服务（fpk 常驻）+ REST API。

形态：飞牛 fpk 原生应用，/app/xbm 网关 + Unix Socket，不暴露 TCP 端口。
M1 先实现最小可用：stdlib ThreadingHTTPServer + /api/ingest + /api/status。

API（见 DESIGN.md §9.3）：
    POST /api/ingest         # {tweets:[...], source, captured_at} → 去重入库
    GET  /api/status         # 服务状态 + 入库统计
    （M4 补全其余接口与前端页面）

鉴权：MVP 依赖飞牛桌面登录（内网使用）；对外暴露时再移植 meizang 登录体系。
CORS：允许扩展跨域 POST（局域网自用）。
"""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config
from .db import Store


def _tweet_info(t: dict[str, Any]) -> dict[str, Any]:
    """扩展推送格式 → 入库格式（防御式）。"""
    media = t.get("media") or []
    return {
        "tweet_id": str(t.get("tweet_id", "")),
        "author": t.get("author", "") or "",
        "screen_name": t.get("screen_name", "") or "",
        "text": t.get("text", "") or "",
        "created_at": t.get("created_at", "") or "",
        "has_article": bool(t.get("has_article")),
        "media_count": len(media),
        "url": t.get("url", "") or "",
    }


def ingest_payload(payload: dict[str, Any], store: Store) -> dict[str, Any]:
    """去重入库，返回统计。幂等：重复推送无副作用。"""
    tweets = payload.get("tweets") or []
    new_count, dup_count = 0, 0
    for t in tweets:
        info = _tweet_info(t)
        tid = info["tweet_id"]
        if not tid:
            continue
        if store.seen(tid):
            dup_count += 1
            continue
        store.record_tweet(info)
        new_count += 1
    run_id = store.start_run()
    store.finish_run(run_id, new_count)
    return {
        "ok": True,
        "new_count": new_count,
        "dup_count": dup_count,
        "total": store.count_tweets(),
    }


class Handler(BaseHTTPRequestHandler):
    config: Config
    store: Store

    def log_message(self, fmt, *args):  # 安静日志，M4 再接日志系统
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status: int, payload: Any):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/status":
            self._json(200, {
                "ok": True,
                "service": "xbm",
                "tweets": self.store.count_tweets(),
                "watermark": self.store.latest_tweet_id(),
                "time": time.time(),
            })
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/api/ingest":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > 20 * 1024 * 1024:  # 20MB 上限
            self._json(400, {"ok": False, "error": "bad body"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid json"})
            return
        try:
            result = ingest_payload(payload, self.store)
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)[:200]})
            return
        self._json(200, result)


def make_server(cfg: Config, port: int) -> ThreadingHTTPServer:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.state_dir / "xbm.db")
    handler = type("XbmHandler", (Handler,), {"config": cfg, "store": store})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server._xbm_store = store  # for graceful shutdown
    return server


def run(cfg: Config, port: int = 8765) -> None:
    server = make_server(cfg, port)
    print(f"xbm serve on :{port}  (data: {cfg.data_root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server._xbm_store.close()
        server.server_close()
