"""Account isolation uses synthetic data and never connects to X."""
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from xbm.accounts import AccountManager
from xbm.config import Config
from xbm.server import make_server


class AccountIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.cfg = Config(data_root=Path(self.tmp.name))
        self.server = make_server(self.cfg, 0, require_auth=False)
        self.manager = self.server._xbm_accounts
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.manager.close()
        self.tmp.cleanup()

    def request(self, path, payload=None, headers=None):
        req = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                      headers=headers or {'Content-Type': 'application/json'})
        with urlopen(req) as response:
            return json.load(response)

    def ingest(self, account, text='Synthetic bookmark'):
        payload = {'account': account, 'tweets': [{'tweet_id': '100', 'text': text,
            'media': [{'type': 'photo', 'url': 'https://pbs.twimg.com/media/synthetic.jpg'}]}]}
        return self.request('/api/ingest', payload)

    def test_same_tweet_can_be_owned_by_two_accounts_and_legacy(self):
        for account in ['', 'x:alpha', 'x:beta']:
            self.assertEqual(self.ingest(account, account)['new_count'], 1)
            self.assertEqual(self.ingest(account)['new_count'], 0)
            rows = self.request('/api/bookmarks?account=' + account)
            self.assertEqual(rows['total'], 1)
            self.assertEqual(rows['items'][0]['text'], account)
            self.assertEqual(len(self.request('/api/runs?account=' + account)['items']), 2)
            self.assertEqual(self.request('/api/status?account=' + account)['media']['pending'], 1)
        self.assertEqual({row['id'] for row in self.request('/api/accounts')['items']}, {'', 'x:alpha', 'x:beta'})

    def test_case_normalization_and_unknown_account_never_falls_back(self):
        self.ingest('x:ALPHA')
        self.assertEqual(self.request('/api/status?account=x:alpha')['tweets'], 1)
        self.assertEqual(self.request('/api/status')['tweets'], 0)
        with self.assertRaises(HTTPError) as error:
            self.request('/api/bookmarks?account=x:missing')
        self.assertEqual(error.exception.code, 404)
        self.assertEqual(len(self.manager.list()), 2)

    def test_paths_restart_discovery_media_and_retry_are_scoped(self):
        for handle in ['alpha', 'beta']:
            self.ingest('x:' + handle)
            cfg, store = self.manager.get('x:' + handle)
            self.assertEqual(cfg.data_root, self.cfg.data_root / 'accounts' / handle)
            self.assertEqual(cfg.media_dir, self.cfg.media_dir / 'accounts' / handle)
            media = store.claim_media(5)
            cfg.media_dir.mkdir(parents=True)
            file = cfg.media_dir / 'synthetic.jpg'
            file.write_bytes(handle.encode())
            store.mark_done(media['id'], str(file), len(handle))
            with urlopen(self.base + '/api/media/1?account=x:' + handle) as response:
                self.assertEqual(response.read(), handle.encode())
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base + '/api/media/1')
        self.assertEqual(error.exception.code, 404)
        beta = self.manager.get('x:beta')[1]
        beta.mark_failed(1, 'fixture', 1)
        headers = {'X-XBM-Action': 'retry'}
        with self.assertRaises(HTTPError) as error:
            self.request('/api/downloads/1/retry?account=x:alpha', {}, headers)
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(beta.get_media(1)['status'], 'failed')
        self.assertTrue(self.request('/api/downloads/1/retry?account=x:beta', {}, headers)['ok'])
        reopened = AccountManager(self.cfg)
        try:
            self.assertEqual({a['id'] for a in reopened.list()}, {'', 'x:alpha', 'x:beta'})
            self.assertEqual(reopened.get('x:alpha')[1].count_tweets(), 1)
        finally:
            reopened.close()

    def test_rejects_unsafe_and_conflicting_identity(self):
        for account in ['../../escape', 'x:../bad', {'id': 'alpha'}, 'x:', 'x:' + 'a'*16]:
            with self.assertRaises(HTTPError) as error:
                self.ingest(account)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.request('/api/ingest?account=x:beta', {'account':'x:alpha', 'tweets':[]})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(len(self.manager.list()), 1)

    def test_new_library_starts_downloader_and_shutdown_closes_all(self):
        with patch('xbm.accounts.Downloader') as downloader:
            self.manager.start()
            self.manager.get('x:alpha', create=True)
            self.assertEqual(downloader.call_count, 2)
            self.assertEqual(downloader.return_value.start.call_count, 2)
