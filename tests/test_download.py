"""M2 下载器测试：本地 HTTP 服务器模拟 CDN，无需真实 X 环境。"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from xbm.config import Config
from xbm.db import Store
from xbm.downloader import Downloader, pick_best, to_orig
from xbm.server import ingest_payload


class RangeHandler(BaseHTTPRequestHandler):
    DATA = b""

    def _send_headers(self, n, status=200, content_range=None):
        self.send_response(status)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(n))
        self.send_header("Accept-Ranges", "bytes")
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()

    def do_HEAD(self):
        self._send_headers(len(self.DATA))

    def do_GET(self):
        data = self.DATA
        range_h = self.headers.get("Range")
        if range_h:
            start = int(range_h.split("=")[1].split("-")[0])
            data = data[start:]
            self._send_headers(
                len(data), 206,
                f"bytes {start}-{len(self.DATA) - 1}/{len(self.DATA)}")
        else:
            self._send_headers(len(data))
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class PickBestTest(unittest.TestCase):
    def test_photo_orig(self):
        self.assertEqual(
            to_orig("https://pbs.twimg.com/media/AbC?format=jpg&name=small"),
            "https://pbs.twimg.com/media/AbC?format=jpg&name=orig")
        self.assertEqual(
            to_orig("https://pbs.twimg.com/media/AbC.jpg"),
            "https://pbs.twimg.com/media/AbC.jpg:orig")

    def test_video_best_mp4(self):
        item = {"type": "video", "url": "x", "variants": [
            {"content_type": "video/mp4",
             "url": "https://v/x_480.mp4", "bitrate": 800000},
            {"content_type": "video/mp4",
             "url": "https://v/x_720.mp4", "bitrate": 2000000},
            {"content_type": "application/x-mpegURL",
             "url": "https://v/x.m3u8", "bitrate": 0},
        ]}
        kind, url = pick_best(item)
        self.assertEqual(kind, "video")
        self.assertEqual(url, "https://v/x_720.mp4")

    def test_hls_only(self):
        item = {"type": "video", "url": "x", "variants": [
            {"content_type": "application/x-mpegURL",
             "url": "https://v/x.m3u8", "bitrate": 0},
        ]}
        self.assertEqual(pick_best(item), ("hls", "https://v/x.m3u8"))


class DownloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RangeHandler.DATA = bytes(range(256)) * 40  # 10240 bytes
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(data_root=self.root / "data")
        self.cfg.download.dir = str(self.root / "dl")
        self.store = Store(self.root / "data" / ".xbm" / "xbm.db")
        self.dl = Downloader(self.cfg, self.store)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.dl.stop()
        self.store.close()
        self.tmp.cleanup()

    def _tweet(self, tid, media):
        return {"tweet_id": tid, "text": "t", "created_at": "c",
                "author": "a", "screen_name": "a",
                "url": f"https://x.com/a/status/{tid}",
                "media": media}

    def test_full_flow(self):
        payload = {"tweets": [self._tweet("111", [
            {"type": "photo", "url": f"{self.base}/img1",
             "expanded_url": "", "variants": []},
        ])]}
        res = ingest_payload(payload, self.store)
        self.assertEqual(res["media_queued"], 1)

        self.assertEqual(self.dl.run_once(), 1)
        f = self.root / "dl" / "111" / "111_1.jpg"
        self.assertTrue(f.exists())
        self.assertEqual(f.stat().st_size, len(RangeHandler.DATA))

        stats = self.store.media_stats()
        self.assertEqual(stats["done"], 1)

        meta = json.loads(
            (self.root / "dl" / "111" / "meta.json").read_text())
        self.assertEqual(meta["tweet_id"], "111")
        self.assertEqual(len(meta["media"]), 1)
        self.assertTrue(meta["media"][0]["local_path"].endswith("111_1.jpg"))

        # 幂等：重复 ingest 不重复下载
        res2 = ingest_payload(payload, self.store)
        self.assertEqual(res2["new_count"], 0)
        self.assertEqual(self.dl.run_once(), 0)

    def test_resume(self):
        payload = {"tweets": [self._tweet("222", [
            {"type": "photo", "url": f"{self.base}/img2",
             "expanded_url": "", "variants": []},
        ])]}
        ingest_payload(payload, self.store)
        # 模拟中断：留下 1000 字节的 .part
        dest_dir = self.root / "dl" / "222"
        dest_dir.mkdir(parents=True, exist_ok=True)
        part = dest_dir / "222_1.jpg.part"
        part.write_bytes(RangeHandler.DATA[:1000])

        self.assertEqual(self.dl.run_once(), 1)
        f = dest_dir / "222_1.jpg"
        self.assertEqual(f.stat().st_size, len(RangeHandler.DATA))
        self.assertEqual(f.read_bytes(), RangeHandler.DATA)

    def test_failed_retry(self):
        payload = {"tweets": [self._tweet("333", [
            {"type": "photo", "url": "http://127.0.0.1:1/none",
             "expanded_url": "", "variants": []},
        ])]}
        ingest_payload(payload, self.store)
        self.assertEqual(self.dl.run_once(), 1)  # 连接被拒绝 → 失败
        stats = self.store.media_stats()
        # max_retries=5 未用完 → 回到 pending 等待重试
        self.assertEqual(stats["pending"], 1)
        rows = self.store.media_for_tweet("333")
        self.assertEqual(rows[0]["retries"], 1)
        self.assertTrue(rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
