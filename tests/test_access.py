"""Default-secure HTTP service, synthetic accounts only."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from xbm.config import Config
from xbm.server import make_server
from xbm.access import Access, digest


class AccessHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_root=Path(self.tmp.name))
        self.server = make_server(self.cfg, 0)
        self.access = self.server._xbm_access
        self.key = self.access.key_path.read_text().strip()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        self.server._xbm_migrations.close(); self.server._xbm_accounts.close()
        self.tmp.cleanup()

    def call(self, path, body=None, token=None, cookie=None, headers=None):
        request_headers = {'Content-Type':'application/json', **(headers or {})}
        if token: request_headers['Authorization'] = 'Bearer '+token
        if cookie: request_headers['Cookie'] = cookie
        req = Request(self.base+path, data=json.dumps(body).encode() if body is not None else None, headers=request_headers)
        with urlopen(req) as response:
            return json.load(response), response.headers

    def denied(self, path, code, **kwargs):
        with self.assertRaises(HTTPError) as error: self.call(path, **kwargs)
        self.assertEqual(error.exception.code, code)
        self.assertNotIn(self.key, error.exception.read().decode())

    def pair(self):
        code = self.call('/api/access/pair-code', {}, self.key, headers={'X-XBM-Action':'access'})[0]['code']
        result = self.call('/api/access/pair', {'code':code, 'name':'Synthetic browser'})[0]
        return code, result

    def test_private_routes_require_auth_including_media_and_migrations(self):
        for path in ['/api/accounts','/api/status','/api/bookmarks','/api/media/1',
                     '/api/storage','/api/diagnostics','/api/migrations','/api/browser-account','/api/client/status']:
            self.denied(path, 401)
        self.denied('/api/ingest', 401, body={'tweets':[]})
        self.denied('/api/bookmarks/delete',401,body={'ids':['1']})
        health, _ = self.call('/api/health')
        self.assertTrue(health['pairing_required'])
        self.assertNotIn('media_dir',health)
        with urlopen(self.base+'/') as response:
            page = response.read().decode()
            self.assertIn('login-dialog',page)
            self.assertNotIn(self.key,page)
        self.assertFalse(self.call('/api/access/status')[0]['authenticated'])

    def test_login_csrf_cookie_and_logout(self):
        self.denied('/api/access/login',403,body={'key':self.key})
        self.denied('/api/access/login',403,body={'key':self.key},headers={'X-XBM-Action':'access','Origin':'https://evil.invalid'})
        self.denied('/api/access/login',400,body={'key':'incorrect'},headers={'X-XBM-Action':'access'})
        result, headers = self.call('/api/access/login',{'key':self.key},headers={'X-XBM-Action':'access','Origin':self.base})
        self.assertNotIn('token',result)
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly',cookie); self.assertIn('SameSite=Strict',cookie)
        cookie = cookie.split(';')[0]
        self.assertTrue(self.call('/api/access/status',cookie=cookie)[0]['authenticated'])
        self.assertTrue(self.call('/api/accounts',cookie=cookie)[0]['ok'])
        self.denied('/api/ingest',403,body={'tweets':[]},cookie=cookie)
        self.call('/api/access/logout',{},cookie=cookie,headers={'X-XBM-Action':'access'})
        self.denied('/api/accounts',401,cookie=cookie)

    def test_pairing_single_use_scoped_revocable_and_persistent(self):
        code, result = self.pair()
        token = result['token']
        self.denied('/api/access/pair',400,body={'code':code})
        self.call('/api/ingest',{'account':'x:fixture','tweets':[{'tweet_id':'123','text':'synthetic'}]},token)
        status = self.call('/api/client/status?account=x:fixture',token=token)[0]
        self.assertEqual(status['tweets'],1)
        self.assertNotIn('media_dir',status)
        self.call('/api/browser-account',{'account':'x:fixture','observed_at':1},token)
        for path in ['/api/accounts','/api/bookmarks','/api/status','/api/access/clients','/api/media/1','/api/storage']:
            self.denied(path,403,token=token)
        self.denied('/api/bookmarks/delete',403,body={'ids':['123']},token=token,headers={'X-XBM-Action':'bookmarks'})
        self.denied('/api/storage/policy',403,body={'mode':'all','budget':0,'paused':False},token=token,headers={'X-XBM-Action':'storage'})
        self.denied('/api/bookmarks/purge',403,body={'ids':['123']},token=token,headers={'X-XBM-Action':'bookmarks'})
        self.denied('/api/access/pair-code',403,body={},token=token,headers={'X-XBM-Action':'access'})
        fresh = Access(self.cfg.state_dir)
        self.assertEqual(fresh.authenticate(token)['role'],'extension')
        clients = self.call('/api/access/clients',token=self.key)[0]['items']
        self.assertEqual(clients[0]['name'],'Synthetic browser')
        self.assertNotIn('token',clients[0]); self.assertNotIn('hash',clients[0])
        with sqlite3.connect(self.access.path) as db:
            stored = db.execute('select hash from clients').fetchone()[0]
            self.assertEqual(stored,digest(token)); self.assertNotEqual(stored,token)
        self.call('/api/access/revoke',{'id':result['id']},self.key,headers={'X-XBM-Action':'access'})
        self.denied('/api/client/status',401,token=token)
        self.assertIsNone(fresh.authenticate(token))

    def test_expiry_new_code_invalidates_previous_and_local_reset(self):
        old = self.access.pairing_code()['code']
        new = self.access.pairing_code()['code']
        self.denied('/api/access/pair',400,body={'code':old})
        with self.access.db() as db: db.execute('UPDATE pairing SET expires=0')
        self.denied('/api/access/pair',400,body={'code':new})
        _, result = self.pair()
        self.access.reset()
        self.assertFalse(self.access.master_matches(self.key))
        self.denied('/api/client/status',401,token=result['token'])
        self.denied('/api/accounts',401,token=self.key)
        self.assertEqual(self.access.key_path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.access.path.stat().st_mode & 0o777,0o600)

    def test_dashboard_delete_restore_and_storage_policy(self):
        self.call('/api/ingest',{'tweets':[{'tweet_id':'555','text':'synthetic'}]},self.key)
        headers={'X-XBM-Action':'bookmarks','Origin':'https://nas.example','Sec-Fetch-Site':'same-origin'}
        self.call('/api/bookmarks/delete',{'ids':['555']},self.key,headers=headers)
        self.assertEqual(self.call('/api/bookmarks?trash=1',token=self.key)[0]['total'],1)
        self.call('/api/bookmarks/restore',{'ids':['555']},self.key,headers=headers)
        self.assertEqual(self.call('/api/bookmarks',token=self.key)[0]['total'],1)
        self.call('/api/bookmarks/delete',{'ids':['555']},self.key,headers=headers)
        self.call('/api/bookmarks/purge',{'ids':['555']},self.key,headers=headers)
        self.assertEqual(self.call('/api/bookmarks?trash=1',token=self.key)[0]['total'],0)
        self.call('/api/storage/policy',{'mode':'photos','budget':1000,'paused':True},self.key,
                  headers={'X-XBM-Action':'storage','Origin':self.base})
        policy=self.call('/api/storage',token=self.key)[0]['policy']
        self.assertEqual(policy['mode'],'photos');self.assertTrue(policy['paused'])

    def test_rate_limit_and_empty_malformed_login(self):
        for _ in range(10):
            self.denied('/api/access/pair',400,body={'code':'invalid'})
        self.denied('/api/access/pair',429,body={'code':'invalid'})

    def test_concurrent_pair_consumption_mints_only_one_credential(self):
        from concurrent.futures import ThreadPoolExecutor
        code = self.access.pairing_code()['code']
        def redeem(_):
            try: return self.access.pair(code,'test','test')['token']
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(token is not None for token in pool.map(redeem,range(2))),1)

    def test_one_use_intent_proof_handles_rewritten_headers_without_cors_exposure(self):
        import urllib.request
        with urllib.request.urlopen(self.base+'/api/dashboard-token') as response:
            self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))
            proof=json.load(response)['token']
        _,headers=self.call('/api/access/login',{'key':self.key},headers={'X-XBM-Action':'access'})
        cookie=headers['Set-Cookie'].split(';')[0]
        self.call('/api/ingest',{'tweets':[{'tweet_id':'777','text':'synthetic'}]},self.key)
        action={'X-XBM-Action':'bookmarks','X-XBM-Dashboard':proof,'Origin':'null','Sec-Fetch-Site':'cross-site'}
        self.call('/api/bookmarks/delete',{'ids':['777']},cookie=cookie,headers=action)
        self.denied('/api/bookmarks/restore',403,body={'ids':['777']},cookie=cookie,headers=action)
        self.assertEqual(self.call('/api/bookmarks?trash=1',token=self.key)[0]['total'],1)

class OriginChecks(unittest.TestCase):
    def test_reverse_proxy_host_rewrite_retains_browser_same_origin_evidence(self):
        from types import SimpleNamespace
        from xbm.web import same_origin
        def check(**headers):
            return same_origin(SimpleNamespace(headers={'X-XBM-Action':'bookmarks', **headers}), 'bookmarks')
        self.assertTrue(check(Origin='http://nas.example',Host='nas.example:80'))
        self.assertTrue(check(Origin='http://NAS.EXAMPLE',Host='nas.example'))
        self.assertTrue(check(Origin='https://nas.example',Host='127.0.0.1:8765',**{'Sec-Fetch-Site':'same-origin'}))
        self.assertFalse(check(Origin='https://evil.example',Host='nas.example',**{'Sec-Fetch-Site':'cross-site'}))
        self.assertFalse(check(Origin='https://evil.example',Host='nas.example',**{'X-Forwarded-Host':'evil.example'}))
        self.assertFalse(check(Origin='null',Host='nas.example',**{'Sec-Fetch-Site':'same-origin'}))
        self.assertFalse(check(Origin='https://nas.example:invalid',Host='nas.example'))

class PasswordProvisioning(unittest.TestCase):
    def test_wizard_password_is_hashed_preserved_and_reset_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            access = Access(Path(tmp))
            password = 'Synthetic-wizard-password-2026'
            self.assertTrue(access.configure_password(password))
            self.assertTrue(access.password_matches(password))
            self.assertFalse(access.configure_password('Other-password-12345'))
            self.assertTrue(access.password_matches(password))
            self.assertFalse(access.password_matches('Other-password-12345'))
            self.assertNotIn(password.encode(),access.path.read_bytes())
            self.assertEqual(access.authenticate(access.login(password,'test')['token'])['role'],'admin')
            with self.assertRaises(ValueError): access.configure_password('short')
            access.reset()
            self.assertFalse(access.password_matches(password))
