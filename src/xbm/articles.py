"""长文章全文 -> Markdown。

优先级：
1. twifork/twikit 的 article 接口（若当前版本可用）；
2. 直接打 TweetDetail GraphQL（带 article 字段开关）；
3. 兜底：Playwright 真浏览器（bookmark-eXport 思路），仅手动 fallback。

输出：$DATA_ROOT/YYYY/MM/<tweet_id>/article.md
     （标题 / 作者 / 发布时间 / 原链 / 正文）

文章失败不阻塞媒体同步，记 flag 下轮重试。

TODO(M3)
"""
from __future__ import annotations


async def fetch_article(tweet_id: str) -> str | None:
    raise NotImplementedError("TODO(M3)")
