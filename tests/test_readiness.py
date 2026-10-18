import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from xbm.backup import create_snapshot, verify_snapshot
from xbm.config import Config, ProxyConfig
from xbm.diagnostics import report


class ReadinessTests(unittest.TestCase):
    def test_backup_includes_wal_and_accounts_and_detects_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root/'config.toml'
            config.write_text('[data]\nroot = '+json.dumps(str(root/'data')))
            dbpath = root/'data/accounts/fixture/.xbm/xbm.db'
            dbpath.parent.mkdir(parents=True)
            with sqlite3.connect(dbpath) as db:
                db.execute('pragma journal_mode=wal')
                db.execute('create table tweets(id text)')
                db.execute("insert into tweets values ('42')")
                db.commit()
                destination = root/'backup'
                create_snapshot(config, destination)
            manifest = verify_snapshot(destination)
            self.assertFalse(manifest['media_included'])
            copy = destination/'databases/accounts/fixture/.xbm/xbm.db'
            with sqlite3.connect(copy) as db:
                self.assertEqual(db.execute('select id from tweets').fetchone()[0], '42')
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(ValueError): create_snapshot(config, destination)
            copy.write_bytes(b'corrupt')
            with self.assertRaises(ValueError): verify_snapshot(destination)

    def test_diagnostics_redacts_credentials_and_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(data_root=Path(tmp)/'private-handle', proxy=ProxyConfig(url='http://user:secret@host'))
            result = report(cfg)
            serialized = json.dumps(result)
            for secret in ('private-handle', 'secret', 'http://user', tmp):
                self.assertNotIn(secret, serialized)
            self.assertTrue(result['proxy_configured'])
            self.assertTrue(all(check['writable'] for check in result['checks']))
            self.assertEqual(list(cfg.media_dir.iterdir()), [])

    def test_deployment_check_detects_old_version_even_with_http_success(self):
        import importlib.util
        from unittest.mock import patch
        import io
        spec = importlib.util.spec_from_file_location('deployment_check', Path(__file__).parents[1]/'scripts/check_deployment.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = {'ok':True, 'service':'xbm', 'version':'0.4.1'}
        with patch.object(module, 'urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
            with self.assertRaisesRegex(ValueError, 'did not take effect'):
                module.verify('http://nas:8765', '0.5.0')
        payload['version']='0.5.0'
        with patch.object(module, 'urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
            self.assertTrue(module.verify('http://nas:8765', '0.5.0')['ok'])

    def test_restore_into_new_library_preserves_accounts_and_media_without_touching_source(self):
        from xbm.accounts import AccountManager
        from xbm.server import ingest_payload
        from xbm.backup import restore_snapshot
        from xbm.config import load
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=root/'config.toml'
            config.write_text('[data]\nroot='+json.dumps(str(root/'original')))
            manager=AccountManager(load(config));cfg,store=manager.get('x:fixture',create=True)
            ingest_payload({'tweets':[{'tweet_id':'42','text':'synthetic','media':[{'type':'photo','url':'https://pbs.twimg.com/media/synthetic.jpg'}]}]},store)
            file=cfg.media_dir/'42/42_1.jpg';file.parent.mkdir(parents=True);file.write_bytes(b'fixture')
            store.mark_done(1,str(file),7)
            create_snapshot(config,root/'backup')
            result=restore_snapshot(root/'backup',root/'restored',root/'original/media')
            self.assertEqual(result['restored_media'],1)
            new=AccountManager(load(root/'restored/config.toml'))
            newcfg,newstore=new.get('x:fixture')
            self.assertEqual(newstore.count_tweets(),1)
            self.assertEqual(newstore.get_media(1)['status'],'done')
            self.assertEqual(Path(newstore.get_media(1)['local_path']).read_bytes(),b'fixture')
            self.assertTrue(Path(newstore.get_media(1)['local_path']).is_relative_to(root/'restored'))
            self.assertEqual(file.read_bytes(),b'fixture')
            new.close()
            missing=restore_snapshot(root/'backup',root/'without-media')
            self.assertEqual(missing['media_pending'],1)
            third=AccountManager(load(root/'without-media/config.toml'))
            self.assertEqual(third.get('x:fixture')[1].get_media(1)['status'],'pending')
            third.close()
            with self.assertRaises(ValueError):restore_snapshot(root/'backup',root/'original')
            manager.close()
