"""Ownership migration, rollback and crash reconciliation on synthetic libraries."""
import json
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from xbm.accounts import AccountManager
from xbm.config import Config
from xbm.migration import Migrations, preview
from xbm.server import ingest_payload, make_server


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(data_root=root/'data', migration_backup_dir=str(root/'backups'))
        self.manager = AccountManager(self.cfg)
        self.manager.get('x:alpha', create=True)
        self.jobs = Migrations(self.manager)
        self.source = self.manager.get('')[1]
        self.target_cfg, self.target = self.manager.get('x:alpha')
        self.payload = {'tweets':[{'tweet_id':'123','text':'Synthetic migration fixture',
            'media':[{'type':'photo','url':'https://pbs.twimg.com/media/test.jpg'}]}]}
        ingest_payload(self.payload, self.source)
        self.file = self.cfg.media_dir/'123'/'123_1.jpg'
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b'synthetic')
        self.source.mark_done(1, str(self.file), 9)

    def tearDown(self):
        self.jobs.close()
        self.manager.close()
        self.tmp.cleanup()

    def execute(self):
        plan = preview(self.manager, 'x:alpha')
        job = self.jobs.start('x:alpha', plan['token'], synchronous=True)
        return next(item for item in self.jobs.list() if item['id']==job)

    def test_complete_backup_counts_files_and_repeat_is_safe(self):
        job = self.execute()
        self.assertEqual(job['status'], 'completed', job['error'])
        self.assertTrue(job['result']['validated'])
        self.assertEqual(self.source.count_tweets(), 0)
        self.assertEqual(self.target.count_tweets(), 1)
        row = self.target.media_for_tweet('123')[0]
        self.assertEqual(Path(row['local_path']).stat().st_ino, self.file.stat().st_ino)
        meta = json.loads((Path(row['local_path']).parent/'meta.json').read_text())
        self.assertEqual(meta['account'], 'x:alpha')
        backup = Path(job['result']['backup'])
        with sqlite3.connect(backup/'source.db') as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tweets').fetchone()[0], 1)
        self.assertTrue((backup/'receipt.json').is_file())
        self.assertEqual(self.execute()['status'], 'failed')
        self.assertEqual(len(self.target.list_runs()['items']), 1)

    def test_duplicates_merge_and_existing_target_metadata_is_preserved(self):
        ingest_payload(self.payload, self.target)
        self.target._conn.execute("UPDATE tweets SET text='Target text',author='Keep author' WHERE tweet_id='123'")
        self.target._conn.commit()
        plan = preview(self.manager, 'x:alpha')
        self.assertEqual(plan['duplicate_tweets'], 1)
        self.assertEqual(plan['duplicate_media'], 1)
        self.assertEqual(self.execute()['status'], 'completed')
        self.assertEqual(len(self.target.media_for_tweet('123')), 1)
        self.assertEqual(self.target.get_tweet('123')['text'], 'Target text')
        self.assertEqual(len(self.target.list_runs()['items']), 2)

    def test_busy_missing_and_stale_preview_never_clear_source(self):
        self.target.add_media('123','photo','https://pbs.twimg.com/other.jpg')
        self.target.claim_media(5)
        self.assertTrue(preview(self.manager,'x:alpha')['busy'])
        self.assertEqual(self.execute()['status'], 'failed')
        self.assertEqual(self.source.count_tweets(), 1)
        self.target.recover_interrupted_media()
        plan = preview(self.manager,'x:alpha')
        self.source.record_tweet({'tweet_id':'999'})
        job = self.jobs.start('x:alpha',plan['token'],synchronous=True)
        self.assertIn('重新预览',self.jobs.list()[0]['error'])
        self.file.unlink()
        self.assertTrue(preview(self.manager,'x:alpha')['errors'])
        self.assertEqual(self.execute()['status'],'failed')
        self.assertEqual(self.source.count_tweets(), 2)

    def test_link_failure_rolls_back_databases_and_preserves_originals(self):
        with patch('xbm.migration.os.link',side_effect=OSError('simulated filesystem error')):
            job=self.execute()
        self.assertEqual(job['status'],'failed')
        self.assertEqual(self.source.count_tweets(),1)
        self.assertEqual(self.target.count_tweets(),0)
        self.assertEqual(self.file.read_bytes(),b'synthetic')
        self.assertEqual(list(self.target_cfg.media_dir.glob('**/*_import_*')),[])

    def test_metadata_failure_restores_existing_metadata_and_new_links(self):
        directory=self.target_cfg.media_dir/'123';directory.mkdir(parents=True)
        metadata=directory/'meta.json';metadata.write_text('old metadata')
        original=Path.write_text
        def fail(path,*args,**kwargs):
            if path==metadata: raise OSError('simulated metadata write failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'write_text',fail):job=self.execute()
        self.assertEqual(job['status'],'failed')
        self.assertEqual(metadata.read_text(),'old metadata')
        self.assertEqual(self.source.count_tweets(),1)
        self.assertEqual(self.target.count_tweets(),0)

    def test_restart_recognizes_committed_marker_without_replaying(self):
        job=self.execute()
        self.jobs.update(job['id'],'running',job['result'])
        reopened=Migrations(self.manager)
        self.assertEqual(reopened.list()[0]['status'],'completed')
        self.assertEqual(len(self.target.list_runs()['items']),1)

    def test_restart_cleans_uncommitted_links_and_records_interruption(self):
        job='b'*32
        directory=self.target_cfg.media_dir/'123';directory.mkdir(parents=True)
        file=directory/f'123_import_{job}_1.jpg';file.write_bytes(b'synthetic')
        with self.jobs.connect() as conn:
            conn.execute("INSERT INTO migrations(id,account,status,created_at,updated_at,result) VALUES (?,?,?,1,1,?)",
                         (job,'x:alpha','running',json.dumps({'created_files':[str(file)]})))
        reopened=Migrations(self.manager)
        self.assertEqual(reopened.list()[0]['status'],'interrupted')
        self.assertFalse(file.exists())
        self.assertEqual(self.source.count_tweets(),1)

    def test_unknown_target_and_empty_target_are_rejected(self):
        for key in ['', 'x:missing', '../unsafe']:
            with self.assertRaises((ValueError,KeyError)):preview(self.manager,key)

    def test_browser_observation_is_durable_without_collecting_bookmarks(self):
        observed=self.jobs.observe('x:BETA')
        self.assertEqual(observed['account'],'x:beta')
        reopened=Migrations(self.manager)
        self.assertEqual(reopened.observed()['account'],'x:beta')
        self.assertEqual(self.manager.get('x:beta')[1].count_tweets(),0)

    def test_rollback_restores_both_libraries_and_metadata(self):
        directory=self.target_cfg.media_dir/'123';directory.mkdir(parents=True)
        meta=directory/'meta.json';meta.write_text('before migration')
        job=self.execute()
        self.assertEqual(job['status'],'completed')
        self.assertEqual(self.jobs.rollback(job['id'])['status'],'rolled_back')
        self.assertEqual(self.source.count_tweets(),1)
        self.assertEqual(self.target.count_tweets(),0)
        self.assertEqual(meta.read_text(),'before migration')
        self.assertEqual(list(directory.glob('*_import_*')),[])
        self.assertEqual(self.file.read_bytes(),b'synthetic')

    def test_rollback_refuses_to_overwrite_subsequent_data(self):
        job=self.execute()
        self.target.record_tweet({'tweet_id':'999'})
        with self.assertRaisesRegex(ValueError,'新数据'):
            self.jobs.rollback(job['id'])
        self.assertEqual(self.target.count_tweets(),2)
        self.assertEqual(self.source.count_tweets(),0)

    def test_post_commit_receipt_failure_is_not_replayed_as_failure(self):
        original=Path.write_text
        def fail(path,*args,**kwargs):
            if path.name=='receipt.json':raise OSError('receipt filesystem failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'write_text',fail):job=self.execute()
        self.assertEqual(job['status'],'completed')
        self.assertEqual(self.target.count_tweets(),1)
        self.assertEqual(self.source.count_tweets(),0)
        self.assertTrue(Path(self.target.media_for_tweet('123')[0]['local_path']).is_file())



class MigrationHTTP(unittest.TestCase):
    def test_api_requires_action_header_and_preserves_journal(self):
        with TemporaryDirectory() as tmp:
            server=make_server(Config(data_root=Path(tmp),migration_backup_dir=str(Path(tmp)/"backups")),0)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            base=f'http://127.0.0.1:{server.server_port}'
            def call(path,data=None,headers=None):
                with urlopen(Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers=headers or {'Content-Type':'application/json'})) as response:
                    return json.load(response)
            try:
                self.assertIsNone(call('/api/browser-account')['observed'])
                self.assertEqual(call('/api/browser-account',{'account':'x:alpha'})['observed']['account'],'x:alpha')
                source=server._xbm_accounts.get('')[1]
                ingest_payload({'tweets':[{'tweet_id':'123'}]},source)
                plan=call('/api/migrations/preview?target=x:alpha')
                self.assertTrue(plan['ready'])
                for headers in [{}, {'X-XBM-Action':'migrate','Origin':'https://other.invalid'}]:
                    with self.assertRaises(HTTPError) as error:call('/api/migrations',{'account':'x:alpha','token':plan['token']},headers)
                    self.assertEqual(error.exception.code,403)
                job=call('/api/migrations',{'account':'x:alpha','token':plan['token']},{'X-XBM-Action':'migrate'})
                server._xbm_migrations.close()
                rows=call('/api/migrations')['items']
                self.assertEqual(rows[0]['id'],job['id'])
                self.assertEqual(rows[0]['status'],'completed',rows[0]['error'])
            finally:
                server.shutdown();server.server_close();server._xbm_migrations.close();server._xbm_accounts.close()
