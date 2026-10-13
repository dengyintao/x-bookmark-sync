"""M1 ingest 链路测试：服务端去重入库，无需真实 X 环境。"""
import json
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from xbm import server as server_mod
from xbm.config import Config


def _post(port, tweets):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/ingest",
        data=json.dumps({"tweets": tweets, "source": "test"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


class IngestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        cfg = Config(data_root=Path(cls.tmp.name))
        cls.srv = server_mod.make_server(cfg, 0)  # 0 = 随机空闲端口
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def _t(self, tid):
        return {"tweet_id": tid, "text": "t", "created_at": "c",
                "author": "a", "screen_name": "a"}

    def test_ingest_and_idempotent(self):
        s, body = _post(self.port, [self._t("1"), self._t("2"), self._t("1")])
        self.assertEqual(s, 200)
        self.assertEqual(body["new_count"], 2)
        self.assertEqual(body["dup_count"], 1)
        s, body = _post(self.port, [self._t("1"), self._t("2")])
        self.assertEqual(s, 200)
        self.assertEqual(body["new_count"], 0)
        self.assertEqual(body["dup_count"], 2)

    def test_status(self):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/status", timeout=10
        ) as r:
            body = json.loads(r.read().decode())
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["tweets"], 2)


if __name__ == "__main__":
    unittest.main()
