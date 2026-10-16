"""HTTP ingestion service and bundled NAS management dashboard.

For trusted private networks; no independent authentication.
"""
from __future__ import annotations

import json
import time
import re
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config
from .db import Store
from .downloader import pick_best


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
    if not isinstance(payload, dict) or not isinstance(payload.get("tweets", []), list):
        raise ValueError("tweets must be an array")
    for tweet in payload.get("tweets", []):
        if not isinstance(tweet, dict) or not re.fullmatch(r"[0-9]{1,30}", str(tweet.get("tweet_id", ""))):
            raise ValueError("tweet_id must contain only digits")
        for field in ("author", "screen_name", "text", "created_at", "url"):
            if tweet.get(field) is not None and not isinstance(tweet[field], str):
                raise ValueError(f"{field} must be a string")
        media = tweet.get("media", [])
        if not isinstance(media, list):
            raise ValueError("media must be an array")
        for item in media:
            if not isinstance(item, dict) or not isinstance(item.get("type", "photo"), str):
                raise ValueError("invalid media item")
            variants = item.get("variants", [])
            if not isinstance(variants, list) or any(not isinstance(v, dict) for v in variants):
                raise ValueError("invalid media variants")
            for entry in [item] + variants:
                if not isinstance(entry.get("url", ""), str):
                    raise ValueError("media URL must be a string")
            for v in variants:
                if not isinstance(v.get("bitrate", 0), (int, float)) or not isinstance(v.get("content_type", ""), str):
                    raise ValueError("invalid media variant")
    prepared = []
    for tweet in payload.get("tweets", []):
        info = _tweet_info(tweet)
        info["jobs"] = [picked for item in tweet.get("media", [])
                        if (picked := pick_best(item))]
        prepared.append(info)
    source = payload.get("source", "extension")
    captured_at = payload.get("captured_at", "")
    if not isinstance(source, str) or not isinstance(captured_at, str):
        raise ValueError("source and captured_at must be strings")
    return store.ingest_batch(prepared, source[:100], captured_at[:100])


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
        from . import web
        if web.get(self):
            return
        if self.path == "/api/status":
            self._json(200, {
                "ok": True,
                "service": "xbm",
                "tweets": self.store.count_tweets(),
                "watermark": self.store.latest_tweet_id(),
                "media": self.store.media_stats(),
                "media_dir": str(self.config.media_dir),
                "time": time.time(),
            })
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        from . import web
        if web.post(self):
            return
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
            # HTTP callers may enqueue only X CDN media, never LAN or local URLs.
            if isinstance(payload, dict) and isinstance(payload.get("tweets"), list):
                for tweet in payload["tweets"]:
                    if not isinstance(tweet, dict) or not isinstance(tweet.get("media", []), list):
                        continue  # ingest_payload supplies the validation error
                    for item in tweet.get("media", []):
                        if not isinstance(item, dict):
                            continue
                        entries = [item] + (item.get("variants") if isinstance(item.get("variants"), list) else [])
                        for entry in entries:
                            url = entry.get("url") if isinstance(entry, dict) else None
                            if not isinstance(url, str) or not url:
                                continue
                            parts = urlsplit(url)
                            if parts.scheme != "https" or parts.hostname not in {"pbs.twimg.com", "video.twimg.com"} or parts.username or parts.password or parts.port not in (None, 443):
                                raise ValueError("media URL must use an HTTPS X CDN address")
            result = ingest_payload(payload, self.store)
        except ValueError as e:
            self._json(400, {"ok": False, "error": str(e)[:200]})
            return
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
    from .downloader import Downloader
    server = make_server(cfg, port)
    store = server._xbm_store
    downloader = Downloader(cfg, store)
    downloader.start()
    print(f"xbm serve on :{port}  (data: {cfg.data_root}, media: {cfg.media_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        downloader.stop()
        store.close()
        server.server_close()
