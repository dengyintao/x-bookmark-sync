"""Dashboard integration checks use synthetic bookmarks and local media only."""
import json
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from xbm.config import Config
from xbm.db import Store
from xbm.server import make_server, ingest_payload


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.cfg = Config(data_root=Path(self.tmp.name))
        self.server = make_server(self.cfg, 0, require_auth=False)
        self.store = self.server._xbm_store
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        ingest_payload({'source': 'extension', 'captured_at': '2026-10-17T01:00:00Z', 'tweets': [
            {'tweet_id': '100', 'author': '测试作者', 'text': '<script>alert(1)</script> 100%',
             'media': [{'type': 'photo', 'url': 'https://pbs.twimg.com/media/sample.jpg'}]},
            {'tweet_id': '200', 'author': '另一作者', 'text': '纯文字'}]}, self.store)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def get(self, path):
        with urlopen(self.base + path) as response:
            return json.load(response)

    def test_library_filters_pagination_and_literal_search(self):
        first = self.get('/api/bookmarks?limit=1')
        self.assertEqual(first['total'], 2)
        self.assertEqual(len(first['items']), 1)
        photo = self.get('/api/bookmarks?kind=photo')['items']
        self.assertEqual(photo[0]['tweet_id'], '100')
        self.assertEqual(len(photo[0]['media']), 1)
        self.assertEqual(self.get('/api/bookmarks?kind=text')['items'][0]['tweet_id'], '200')
        self.assertEqual(self.get('/api/bookmarks?q=%25')['total'], 1)
        self.assertEqual(self.get('/api/bookmarks?q=%27%20OR%201%3D1--')['total'], 0)
        self.assertEqual(self.get('/api/bookmarks?offset=100')['items'], [])

    def test_receipts_distinguish_received_duplicates_and_queued(self):
        ingest_payload({'tweets': [{'tweet_id': '100'}]}, self.store)
        rows = self.get('/api/runs')['items']
        self.assertEqual((rows[0]['received_count'], rows[0]['new_count'], rows[0]['dup_count']), (1, 0, 1))
        self.assertEqual(rows[1]['media_queued'], 1)
        self.assertEqual(rows[1]['source'], 'extension')
        self.assertEqual(rows[1]['captured_at'], '2026-10-17T01:00:00Z')

    def test_progress_and_retry_guards(self):
        job = self.store.claim_media(5)
        self.store.update_progress(job['id'], 500, 1000, 250)
        row = self.get('/api/downloads?status=downloading')['items'][0]
        self.assertEqual((row['bytes'], row['total_bytes'], row['speed']), (500, 1000, 250))
        self.store.mark_failed(job['id'], 'simulated failure', 1)
        path = self.base + f"/api/downloads/{job['id']}/retry"
        for headers in [{}, {'X-XBM-Action': 'retry', 'Origin': 'https://other.invalid'},
                        {'X-XBM-Action': 'retry', 'Sec-Fetch-Site': 'cross-site'}]:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(path, method='POST', headers=headers))
            self.assertEqual(error.exception.code, 403)
        with urlopen(Request(path, method='POST', headers={'X-XBM-Action': 'retry', 'Origin': self.base})) as response:
            self.assertTrue(json.load(response)['ok'])
        row = self.store.get_media(job['id'])
        self.assertEqual((row['status'], row['retries'], row['next_retry_at']), ('pending', 0, 0))

    def test_media_ranges_and_path_isolation(self):
        job = self.store.claim_media(5)
        self.cfg.media_dir.mkdir()
        file = self.cfg.media_dir / 'sample.mp4'
        file.write_bytes(b'0123456789')
        self.store.mark_done(job['id'], str(file), 10)
        url = self.base + f"/api/media/{job['id']}"
        with urlopen(Request(url, headers={'Range': 'bytes=2-5'})) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers['Content-Range'], 'bytes 2-5/10')
            self.assertEqual(response.read(), b'2345')
        with urlopen(Request(url, headers={'Range': 'bytes=-3'})) as response:
            self.assertEqual(response.read(), b'789')
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(url, headers={'Range': 'bytes=100-200'}))
        self.assertEqual(error.exception.code, 416)
        outside = Path(self.tmp.name) / 'private.mp4'
        outside.write_bytes(b'secret')
        self.store.mark_done(job['id'], str(outside), 6)
        with self.assertRaises(HTTPError) as error:
            urlopen(url)
        self.assertEqual(error.exception.code, 404)
        link = self.cfg.media_dir / 'escape.mp4'
        link.symlink_to(outside)
        self.store.mark_done(job['id'], str(link), 6)
        with self.assertRaises(HTTPError) as error:
            urlopen(url)
        self.assertEqual(error.exception.code, 404)

    def test_invalid_pagination_and_filters(self):
        for path in ['/api/bookmarks?limit=-1','/api/runs?offset=no','/api/downloads?status=oops', '/api/bookmarks?limit=101']:
            with self.assertRaises(HTTPError) as error:
                self.get(path)
            self.assertEqual(error.exception.code, 400)

    def test_assets_have_csp_and_no_external_dependencies(self):
        for path in ['/', '/app.js', '/style.css', '/icon.svg', '/manifest.webmanifest']:
            with urlopen(self.base + path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("default-src 'self'", response.headers['Content-Security-Policy'])
                self.assertGreater(len(response.read()), 0)
        with self.assertRaises(HTTPError):
            urlopen(self.base + '/../../config.toml')


    def test_delete_restore_filter_and_resync_do_not_resurrect(self):
        def mutate(action, ids, headers=None):
            req = Request(self.base+'/api/bookmarks/'+action, method='POST',
                          data=json.dumps({'ids':ids}).encode(), headers=headers or {'X-XBM-Action':'bookmarks','Content-Type':'application/json'})
            with urlopen(req) as response: return json.load(response)
        self.assertEqual(self.get('/api/bookmarks?status=incomplete')['total'],1)
        self.assertEqual(self.get('/api/bookmarks?status=done')['total'],0)
        with self.assertRaises(HTTPError) as denied:
            mutate('delete',['100'],{'Origin':'https://evil.invalid','X-XBM-Action':'bookmarks'})
        self.assertEqual(denied.exception.code,403)
        mutate('delete',['100'])
        self.assertEqual(self.get('/api/bookmarks')['total'],1)
        self.assertEqual(self.get('/api/bookmarks?trash=1')['items'][0]['tweet_id'],'100')
        self.assertIsNone(self.store.claim_media(5))
        self.assertEqual(self.get('/api/downloads')['total'],0)
        ingest_payload({'tweets':[{'tweet_id':'100'}]},self.store)
        self.assertEqual(self.get('/api/bookmarks')['total'],1)
        mutate('restore',['100'])
        job=self.store.claim_media(5)
        with self.assertRaises(HTTPError): mutate('delete',['100','200'])
        self.assertEqual(self.get('/api/bookmarks')['total'],2)
        self.store.mark_done(job['id'],'unused',10)
        self.assertEqual(self.get('/api/bookmarks?status=done')['total'],1)
        self.assertEqual(self.get('/api/bookmarks?status=incomplete')['total'],0)
        with self.assertRaises(HTTPError): mutate('delete',[])
        with self.assertRaises(HTTPError): self.get('/api/bookmarks?status=invalid')


class MigrationTest(unittest.TestCase):
    def test_old_database_remains_readable_and_history_is_not_invented(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db'
            conn = sqlite3.connect(path)
            conn.executescript('''CREATE TABLE media_files(id INTEGER PRIMARY KEY, tweet_id TEXT,kind TEXT,remote_url TEXT);
                INSERT INTO media_files VALUES(1,'123','photo','https://pbs.twimg.com/a');
                CREATE TABLE sync_runs(id INTEGER PRIMARY KEY,started_at REAL,finished_at REAL,new_count INTEGER,status TEXT,error TEXT);
                INSERT INTO sync_runs VALUES(1,1,1,1,'ok','');''')
            conn.commit()
            conn.close()
            store = Store(path)
            self.assertEqual(store.list_downloads()['items'][0]['total_bytes'], 0)
            run = store.list_runs()['items'][0]
            self.assertEqual(run['source'], 'legacy')
            self.assertIsNone(run['received_count'])
            store.close()
