import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xbm.config import load
from xbm.db import Store
from xbm.server import ingest_payload

class Regressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.db'
        self.store = Store(self.path)
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
    def test_retry_waits_for_backoff(self):
        self.store.add_media('1', 'photo', 'https://pbs.twimg.com/a.jpg')
        job = self.store.claim_media(5)
        self.store.mark_failed(job['id'], 'offline', 5)
        self.assertIsNone(self.store.claim_media(5))
        with patch('xbm.db.time.time', return_value=10**12):
            self.assertIsNotNone(self.store.claim_media(5))
    def test_restart_recovers_interrupted_download(self):
        self.store.add_media('1', 'photo', 'https://pbs.twimg.com/a.jpg')
        self.store.claim_media(5)
        self.store.close()
        self.store = Store(self.path)
        self.assertIsNone(self.store.claim_media(5))  # Opening a reader must not steal a live job.
        self.store.recover_interrupted_media()
        self.assertIsNotNone(self.store.claim_media(5))
    def test_invalid_payload_does_not_partially_write(self):
        for payload in ([], {'tweets': {}}, {'tweets': [{'tweet_id': '1'}, {'tweet_id': '../escape'}]}):
            with self.assertRaises(ValueError):
                ingest_payload(payload, self.store)
        self.assertEqual(self.store.count_tweets(), 0)
    def test_duplicate_can_recover_missing_media(self):
        ingest_payload({'tweets': [{'tweet_id': '1'}]}, self.store)
        ingest_payload({'tweets': [{'tweet_id': '1', 'media': [{'url': 'https://pbs.twimg.com/a.jpg'}]}]}, self.store)
        self.assertEqual(len(self.store.media_for_tweet('1')), 1)
    def test_explicit_config_string(self):
        path = Path(self.tmp.name) / 'config.toml'
        path.write_text('[data]\nroot="/tmp/test-root"\n')
        self.assertEqual(load(str(path)).data_root, Path('/tmp/test-root'))

    def test_media_insert_error_rolls_back_tweet_and_receipt(self):
        self.store._conn.execute("""CREATE TRIGGER fail_media BEFORE INSERT ON media_files
                                  BEGIN SELECT RAISE(ABORT, 'disk error'); END""")
        with self.assertRaises(Exception):
            ingest_payload({'tweets':[{'tweet_id':'42', 'media':[{'url':'https://pbs.twimg.com/a.jpg'}]}]},self.store)
        self.assertEqual(self.store.count_tweets(),0)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM sync_runs').fetchone()[0],0)
    def test_duplicate_receipt_counts_only_new_media(self):
        payload={'tweets':[{'tweet_id':'42','media':[{'url':'https://pbs.twimg.com/a.jpg'}]}]}
        self.assertEqual(ingest_payload(payload,self.store)['media_queued'],1)
        receipt=ingest_payload(payload,self.store)
        self.assertEqual(receipt['media_queued'],0)
        self.assertEqual(receipt['new_count'],0)
    def test_concurrent_duplicate_batches_count_once(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:ingest_payload({'tweets':[{'tweet_id':'42'}]},self.store), range(8)))
        self.assertEqual(sum(r['new_count'] for r in results),1)

class HTTPValidation(unittest.TestCase):
    def test_rejects_invalid_json_shapes_and_private_media(self):
        import json
        import threading
        import urllib.request
        import urllib.error
        from xbm.config import Config
        from xbm.server import make_server
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(Config(data_root=Path(tmp)), 0, require_auth=False)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for payload in ([], {'tweets': {}}, {'tweets': [{'tweet_id': '../../escape'}]},
                                {'tweets': [{'tweet_id': '1', 'media': [{'url': 'http://127.0.0.1/private'}]}]}):
                    req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/ingest',
                          data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(req, timeout=5)
                    self.assertEqual(caught.exception.code, 400)
                self.assertEqual(server._xbm_store.count_tweets(), 0)
            finally:
                server.shutdown()
                server.server_close()
                server._xbm_store.close()
