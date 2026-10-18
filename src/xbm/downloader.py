"""M2 媒体下载器：后台队列消费 media_files，落地到下载目录。

- 图片：pbs.twimg.com 原图直链（name=orig）
- 视频/GIF：选最高码率 MP4 变体
- 仅 HLS（m3u8）时：ffmpeg 转码，gallery-dl 兜底
- 断点续传：.part 临时文件 + HTTP Range
- 失败重试：指数退避，见 Store.mark_failed
- 每条推文媒体齐后写 meta.json

下载目录：{download.dir || data.root/media}/{tweet_id}/
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import httpx

from .config import Config
from .db import Store

_CHUNK = 1024 * 256
_IDLE_SLEEP = 5.0


def to_orig(url: str) -> str:
    """pbs.twimg.com 图片 URL → 原图。"""
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    if parts.hostname == "pbs.twimg.com":
        q = dict(urllib.parse.parse_qsl(parts.query))
        if q:
            q["name"] = "orig"
            return urllib.parse.urlunsplit(
                parts._replace(query=urllib.parse.urlencode(q)))
        # 老式 https://pbs.twimg.com/media/ID.jpg
        if parts.path.endswith((".jpg", ".jpeg", ".png", ".webp")):
            return url + ":orig"
        return url + "?format=jpg&name=orig"
    return url


def pick_best(item: dict) -> tuple[str, str] | None:
    """扩展 media 项 → (kind, 下载 URL)。kind: photo|video|gif|hls。"""
    mtype = (item.get("type") or "photo").lower()
    variants = item.get("variants") or []
    if mtype == "photo" or not variants:
        url = to_orig(item.get("url", ""))
        return ("photo", url) if url else None
    kind = "gif" if mtype == "animated_gif" else "video"
    mp4s = [v for v in variants
            if v.get("url") and "mp4" in (v.get("content_type") or "")]
    if mp4s:
        best = max(mp4s, key=lambda v: v.get("bitrate") or 0)
        return (kind, best["url"])
    hls = [v for v in variants if urllib.parse.urlsplit(v.get("url") or "").path.endswith(".m3u8")]
    if hls:
        return ("hls", hls[0]["url"])
    # 实在没有可用变体，退回原 url
    url = item.get("url", "")
    return (kind, url) if url else None


def _ext_for(kind: str, url: str, content_type: str) -> str:
    if kind in ("video", "gif", "hls"):
        return "mp4"
    path = urllib.parse.urlsplit(url).path.lower()
    for ext in ("jpg", "jpeg", "png", "webp", "gif"):
        if path.endswith("." + ext):
            return "jpg" if ext == "jpeg" else ext
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    if q.get("format") in ("jpg", "png", "webp"):
        return q["format"]
    if "jpeg" in content_type:
        return "jpg"
    if "png" in content_type:
        return "png"
    if "webp" in content_type:
        return "webp"
    return "jpg"


class Downloader:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        proxy = cfg.proxy.url or None
        self._client = httpx.Client(
            proxy=proxy,
            # 只用 [proxy] 配置的代理，不读系统环境变量（应用级代理）
            trust_env=False,
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )

    # ---- 生命周期 ----

    def start(self) -> None:
        self.store.recover_interrupted_media()
        n = max(1, self.cfg.download.workers)
        for i in range(n):
            t = threading.Thread(target=self._loop, name=f"xbm-dl-{i}",
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)
        self._client.close()

    # ---- 主循环 ----

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.cfg.media_dir.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(self.cfg.media_dir).free < 512 * 1024 * 1024:
                self._stop.wait(30)
                continue
            job = self.store.claim_media(self.cfg.download.max_retries)
            if not job:
                self._stop.wait(_IDLE_SLEEP)
                continue
            try:
                self._download(job)
            except Exception as e:  # noqa: BLE001 — 失败进重试队列
                self.store.mark_failed(
                    job["id"], f"{type(e).__name__}: {e}",
                    self.cfg.download.max_retries)

    def run_once(self) -> int:
        """同步处理一个任务（测试用），返回 1=处理了, 0=无任务。"""
        job = self.store.claim_media(self.cfg.download.max_retries)
        if not job:
            return 0
        try:
            self._download(job)
        except Exception as e:  # noqa: BLE001
            self.store.mark_failed(
                job["id"], f"{type(e).__name__}: {e}",
                self.cfg.download.max_retries)
        return 1

    # ---- 下载 ----

    def _download(self, job: dict) -> None:
        tweet_id = job["tweet_id"]
        kind, url = job["kind"], job["remote_url"]
        if not str(tweet_id).isdigit():
            raise ValueError("unsafe tweet_id")
        dest_dir = self.cfg.media_dir / tweet_id
        dest_dir.mkdir(parents=True, exist_ok=True)

        idx = self._media_index(tweet_id, job["id"])
        if kind == "hls":
            final = dest_dir / f"{tweet_id}_{idx}.mp4"
            self._download_hls(url, final)
        else:
            head = self._client.head(url)
            ctype = head.headers.get("content-type", "")
            ext = _ext_for(kind, url, ctype)
            final = dest_dir / f"{tweet_id}_{idx}.{ext}"
            if final.exists() and final.stat().st_size > 0:
                # 已有完整文件：直接标记完成（幂等）
                self.store.mark_done(job["id"], str(final),
                                     final.stat().st_size)
                self._maybe_write_meta(tweet_id)
                return
            self._download_http(url, final, job["id"])

        size = final.stat().st_size
        if size == 0:
            final.unlink(missing_ok=True)
            raise RuntimeError("downloaded 0 bytes")
        self.store.mark_done(job["id"], str(final), size)
        self._maybe_write_meta(tweet_id)

    def _media_index(self, tweet_id: str, media_id: int) -> int:
        items = self.store.media_for_tweet(tweet_id)
        for i, m in enumerate(items, 1):
            if m["id"] == media_id:
                return i
        return 1

    def _download_http(self, url: str, final: Path, media_id: int | None = None) -> None:
        part = final.with_suffix(final.suffix + ".part")
        resume_from = part.stat().st_size if part.exists() else 0
        headers = {}
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
        with self._client.stream("GET", url, headers=headers) as r:
            r.raise_for_status()
            if resume_from and r.status_code == 206:
                content_range = r.headers.get("content-range", "")
                if not content_range.startswith(f"bytes {resume_from}-"):
                    raise RuntimeError("server returned an inconsistent Content-Range")
            mode = "ab" if (resume_from and r.status_code == 206) else "wb"
            received = resume_from if mode == "ab" else 0
            total = int(r.headers.get("content-length", 0)) + received
            # Decoded HTTP bodies can differ from Content-Length.
            if r.headers.get("content-encoding"):
                total = 0
            last_at, last_bytes = time.monotonic(), received
            if media_id is not None:
                self.store.update_progress(media_id, received, total, 0)
            with open(part, mode) as f:
                for chunk in r.iter_bytes(_CHUNK):
                    if shutil.disk_usage(self.cfg.media_dir).free < 128 * 1024 * 1024:
                        raise OSError("Insufficient disk space; download paused")
                    f.write(chunk)
                    received += len(chunk)
                    now = time.monotonic()
                    if media_id is not None and now - last_at >= 0.5:
                        self.store.update_progress(media_id, received, total, (received - last_bytes) / (now - last_at))
                        last_at, last_bytes = now, received
            if media_id is not None:
                self.store.update_progress(media_id, received, total, 0)
        part.rename(final)

    def _download_hls(self, url: str, final: Path) -> None:
        """ffmpeg 转码；失败则 gallery-dl 兜底。"""
        ffmpeg = shutil.which("ffmpeg")
        err = ""
        if ffmpeg:
            try:
                subprocess.run(
                    [ffmpeg, "-y", "-loglevel", "error", "-i", url,
                     "-c", "copy", str(final)],
                    check=True, timeout=600,
                    capture_output=True, text=True,
                )
                return
            except Exception as e:  # noqa: BLE001
                err = str(e)
        # 兜底：gallery-dl
        try:
            subprocess.run(
                ["gallery-dl", "--no-check-certificate", "-q",
                 "-d", str(final.parent), "-f",
                 final.stem + ".{extension}", url],
                check=True, timeout=600,
                capture_output=True, text=True,
            )
            # gallery-dl 落盘文件名不完全可控，找最新文件改名
            newest = max(final.parent.glob(f"{final.stem}.*"),
                         key=lambda p: p.stat().st_mtime, default=None)
            if newest and newest != final:
                newest.rename(final)
            if not final.exists():
                raise RuntimeError("gallery-dl produced no file")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"hls failed (ffmpeg: {err}; gallery-dl: {e})")

    # ---- meta.json ----

    def _maybe_write_meta(self, tweet_id: str) -> None:
        if not self.store.tweet_media_done(tweet_id):
            return
        tweet = self.store.get_tweet(tweet_id) or {}
        media = [
            {"type": m["kind"], "local_path": m["local_path"],
             "remote_url": m["remote_url"], "bytes": m["bytes"]}
            for m in self.store.media_for_tweet(tweet_id)
        ]
        meta = {
            "tweet_id": tweet_id,
            "url": tweet.get("url", ""),
            "author": tweet.get("author", ""),
            "screen_name": tweet.get("screen_name", ""),
            "text": tweet.get("text", ""),
            "created_at": tweet.get("created_at", ""),
            "media": media,
        }
        dest = self.cfg.media_dir / tweet_id / "meta.json"
        dest.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                        encoding="utf-8")
