"""Permanent cleanup tests only use temporary, synthetic files."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from xbm.accounts import AccountManager
from xbm.config import Config
from xbm.storage import Storage
from xbm.server import ingest_payload


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.manager=AccountManager(Config(data_root=Path(self.tmp.name)))
        self.cfg,self.store=self.manager.get('x:alpha',create=True)
        self.storage=Storage(self.manager)
        ingest_payload({'tweets':[{'tweet_id':'100','text':'synthetic', 'media':[{'type':'photo','url':'https://pbs.twimg.com/media/synthetic.jpg'}]}]},self.store)
        self.file=self.cfg.media_dir/'100'/'100_1.jpg';self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b'synthetic')
        self.store.mark_done(1,str(self.file),9)

    def tearDown(self):
        self.manager.close();self.tmp.cleanup()

    def test_only_trash_can_be_purged_and_resync_does_not_download_again(self):
        with self.assertRaises(ValueError): self.storage.purge('x:alpha',['100'])
        self.store.set_deleted(['100'])
        result=self.storage.purge('x:alpha',['100'])
        self.assertEqual(result['purged'],1);self.assertEqual(result['cleanup_pending'],0)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.store.list_bookmarks(trash=True)['total'],0)
        self.assertEqual(self.store.get_tweet('100')['text'],'')
        self.assertEqual(self.store.set_deleted(['100'],True),0)
        ingest_payload({'tweets':[{'tweet_id':'100','text':'synthetic','media':[{'type':'photo','url':'https://pbs.twimg.com/media/synthetic.jpg'}]}]},self.store)
        self.assertEqual(self.store.list_downloads()['total'],0)
        self.assertEqual(self.store.count_tweets(),0)

    def test_hard_link_kept_for_other_account_and_unknown_file_untouched(self):
        cfg2,store2=self.manager.get('x:beta',create=True)
        ingest_payload({'tweets':[{'tweet_id':'100'}]},store2)
        other=cfg2.media_dir/'100'/'100_1.jpg';other.parent.mkdir(parents=True);other.hardlink_to(self.file)
        note=self.file.parent/'personal-note.txt';note.write_text('keep')
        self.store.set_deleted(['100']);self.storage.purge('x:alpha',['100'])
        self.assertEqual(other.read_bytes(),b'synthetic')
        self.assertEqual(note.read_text(),'keep')

    def test_shared_path_reference_and_outside_path_are_protected(self):
        ingest_payload({'tweets':[{'tweet_id':'200','media':[{'type':'photo','url':'https://pbs.twimg.com/media/other.jpg'}]}]},self.store)
        self.store.mark_done(2,str(self.file),9)
        self.store.set_deleted(['100']);self.storage.purge('x:alpha',['100'])
        self.assertTrue(self.file.exists())
        outside=Path(self.tmp.name)/'private.txt';outside.write_text('keep')
        self.store.mark_done(2,str(outside),4);self.store.set_deleted(['200'])
        with self.assertRaises(ValueError): self.storage.purge('x:alpha',['200'])
        self.assertTrue(outside.exists())
        self.assertEqual(self.store.list_bookmarks(trash=True)['total'],1)

    def test_failed_unlink_is_durable_and_can_be_retried(self):
        self.store.set_deleted(['100'])
        original=Path.unlink
        def fail(path,*args,**kwargs):
            if path==self.file:raise PermissionError('test')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail):
            result=self.storage.purge('x:alpha',['100'])
        self.assertEqual(result['cleanup_pending'],1)
        self.assertTrue(self.file.exists())
        fresh=Storage(self.manager);fresh.cleanup('x:alpha')
        self.assertFalse(self.file.exists());self.assertEqual(fresh.summary('x:alpha')['cleanup_pending'],0)

    def test_account_scope_and_partial_files(self):
        _,beta=self.manager.get('x:beta',create=True)
        ingest_payload({'tweets':[{'tweet_id':'100','text':'keep'}]},beta)
        part=self.file.with_suffix('.jpg.part');part.write_bytes(b'partial')
        self.store.set_deleted(['100']);self.storage.purge('x:alpha',['100'])
        self.assertFalse(part.exists())
        self.assertEqual(beta.get_tweet('100')['text'],'keep')

    def test_download_policy_is_persistent_scoped_and_budget_pauses_claims(self):
        self.store.set_download_policy('text',0,False)
        ingest_payload({'tweets':[{'tweet_id':'200','media':[{'type':'photo','url':'https://pbs.twimg.com/media/another.jpg'}]}]},self.store)
        self.assertIsNone(self.store.claim_media(5))
        self.store.set_download_policy('all',1,False)
        self.assertTrue(self.store.download_policy()['budget_reached'])
        self.assertIsNone(self.store.claim_media(5))
        self.store.set_download_policy('all',0,True)
        self.assertIsNone(self.store.claim_media(5))
        self.store.set_download_policy('videos',0,False)
        self.assertIsNone(self.store.claim_media(5))
        self.store.set_download_policy('photos',0,False)
        self.assertEqual(self.store.claim_media(5)['tweet_id'],'200')
        _,beta=self.manager.get('x:beta',create=True)
        self.assertEqual(beta.download_policy()['mode'],'all')
        with self.assertRaises(ValueError):self.store.set_download_policy('invalid',0,False)
        with self.assertRaises(ValueError):self.store.set_download_policy('all',-1,False)
