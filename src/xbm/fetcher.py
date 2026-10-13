"""书签增量抓取（twifork）。

增量逻辑（书签接口无 since_id、无收藏时间戳，只能客户端自己记）：
- 每轮从第一页开始翻，遇到 seen(tweet_id) 即停止；
- 单轮最多 max_pages 页，防止某次书签暴增时跑太久；
- 请求间隔 request_delay + jitter。

视频直链：优先 variants 里码率最高的渐进式 mp4；
HLS-only 的不自己处理，交给 downloader(gallery-dl)。

M1：只打印新增书签列表（不下载）。
M2：把 media 交给 downloader。
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, AsyncIterator

import twikit

from .db import Store


def summarize(tweet: Any) -> dict[str, Any]:
    """抽取入库/展示用字段，防御式读取（X 字段常变）。"""
    user = getattr(tweet, "user", None)
    try:
        media = tweet.media or []
    except Exception:
        media = []
    media_info = []
    for m in media:
        try:
            media_info.append({
                "type": getattr(m, "type", "?"),
                "url": getattr(m, "url", "") or "",
                "source_url": getattr(m, "source_url", "") or "",
            })
        except Exception:
            continue
    text = ""
    for attr in ("full_text", "text"):
        try:
            text = getattr(tweet, attr) or ""
        except Exception:
            continue
        if text:
            break
    return {
        "tweet_id": str(getattr(tweet, "id", "")),
        "author": getattr(user, "name", "") or "",
        "screen_name": getattr(user, "screen_name", "") or "",
        "text": text,
        "created_at": str(getattr(tweet, "created_at", "") or ""),
        "media": media_info,
        "media_count": len(media_info),
        # X Article 判定：M3 实现，此处先占位
        "has_article": False,
    }


def best_video_url(video: Any) -> str:
    """选 variants 里码率最高的渐进式 mp4；没有则返回空（HLS-only 交给 gallery-dl）。"""
    try:
        variants = (video.video_info or {}).get("variants", [])
    except Exception:
        return ""
    best, best_br = "", -1
    for v in variants:
        url = v.get("url", "")
        if v.get("content_type") == "video/mp4" and "/audio/" not in url:
            br = v.get("bitrate", 0) or 0
            if br > best_br:
                best, best_br = url, br
    return best


async def fetch_new_bookmarks(
    client: twikit.Client,
    store: Store,
    page_size: int = 20,
    max_pages: int = 10,
    delay: float = 1.5,
) -> AsyncIterator[dict[str, Any]]:
    """增量抓取：yield 新增书签的 summarize() 结果，遇到已见 id 即停。"""
    page = await client.get_bookmarks(count=page_size)
    for _ in range(max_pages):
        if not page:
            break
        stop = False
        for tweet in page:
            tid = str(getattr(tweet, "id", ""))
            if not tid:
                continue
            if store.seen(tid):
                stop = True
                break
            yield summarize(tweet)
        if stop:
            break
        await asyncio.sleep(delay + random.uniform(0, delay))
        page = await page.next()


async def run_sync(client: twikit.Client, store: Store,
                   page_size: int, max_pages: int, delay: float) -> list[dict]:
    found: list[dict] = []
    async for info in fetch_new_bookmarks(client, store, page_size, max_pages, delay):
        store.record_tweet(info)
        found.append(info)
    return found
