"""媒体下载（gallery-dl 子进程）。

- 逐条推文调用：gallery-dl --cookies cookies.txt -d <dest> <tweet_url>
- 文件命名 <tweet_id>_p<N>.<ext> / <tweet_id>_video.mp4，已存在跳过（幂等）。
- HLS(m3u8)：gallery-dl 自动拉分片 + ffmpeg remux，需系统装好 ffmpeg。
- 子进程超时 + 失败重试 1 次，仍失败则记入 db 下轮补。

TODO(M2)
"""
from __future__ import annotations


def download_tweet_media(tweet_url: str, dest_dir: str) -> list[str]:
    raise NotImplementedError("TODO(M2)")
