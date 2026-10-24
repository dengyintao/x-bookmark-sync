"""Synthetic fnOS gateway requests; kernel peer identity and NAS role are required."""
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from xbm.config import Config
from xbm.server import make_server
from xbm.gateway import make_gateway, PREFIX


@unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'Unix transport required')
class GatewayHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='xbm-gw-', dir='/tmp')
        self.root = Path(self.tmp.name)
        self.http = make_server(Config(data_root=self.root/'data'), 0)
        self.peer_patch = None
        if not hasattr(socket, 'SO_PEERCRED'):
            self.peer_patch = patch('xbm.gateway.peer_uid', return_value=os.getuid())
            self.peer_patch.start()
        self.gateway = make_gateway(self.http, self.root/'app.sock', allowed_uids={os.getuid()})
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def tearDown(self):
        self.gateway.shutdown(); self.gateway.server_close()
        self.http.shutdown(); self.http.server_close()
        self.http._xbm_migrations.close(); self.http._xbm_accounts.close()
        if self.peer_patch: self.peer_patch.stop()
        self.tmp.cleanup()

    def call(self, path, body=None, headers=None, *, tcp=False):
        if tcp:
            connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
        else:
            connection = http.client.HTTPConnection('gateway')
            connection.sock = socket.socket(socket.AF_UNIX)
            connection.sock.connect(str(self.root/'app.sock'))
        with closing(connection):
            connection.request('POST' if body is not None else 'GET', path,
                body=json.dumps(body) if body is not None else None,
                headers={'X-Trim-Userid':'1000','X-Trim-Isadmin':'true','Content-Type':'application/json',**(headers or {})})
            response = connection.getresponse()
            return response.status, response.read(), response.headers

    def test_nas_session_needs_no_application_cookie_even_with_password_enabled(self):
        self.http._xbm_access.set_security(True,'Synthetic-password-2026')
        code, body, headers = self.call(PREFIX+'/api/access/status')
        result = json.loads(body)
        self.assertEqual(code,200)
        self.assertTrue(result['authenticated']); self.assertTrue(result['nas_session'])
        self.assertEqual(result['role'],'admin')
        self.assertIsNone(headers.get('Set-Cookie'))
        self.assertEqual(self.call(PREFIX+'/api/accounts')[0],200)
        # Identical headers over the public service port do not establish NAS identity.
        self.assertEqual(self.call('/api/accounts',tcp=True)[0],401)
        self.assertFalse(json.loads(self.call('/api/access/status',tcp=True)[1])['nas_session'])

    def test_assets_manifest_and_queries_keep_gateway_prefix(self):
        code, html, _ = self.call(PREFIX+'/')
        self.assertEqual(code,200)
        self.assertIn((PREFIX+'/app.js').encode(),html)
        self.assertNotIn(b'src="/app.js"',html)
        manifest=json.loads(self.call(PREFIX+'/manifest.webmanifest')[1])
        self.assertEqual(manifest['start_url'],PREFIX+'/')
        self.assertEqual(manifest['icons'][0]['src'],PREFIX+'/icon.svg')
        self.assertEqual(json.loads(self.call(PREFIX+'/api/runs?limit=1')[1])['total'],0)
        self.assertEqual(self.call('/api/accounts')[0],404)
        self.assertEqual(self.call(PREFIX+'-other/api/accounts')[0],404)

    def test_missing_identity_nonadmin_and_untrusted_kernel_peer_are_denied(self):
        self.assertEqual(self.call(PREFIX+'/',headers={'X-Trim-Isadmin':'false'})[0],403)
        self.assertEqual(self.call(PREFIX+'/api/accounts',headers={'X-Trim-Userid':''})[0],403)
        with patch('xbm.gateway.peer_uid',return_value=os.getuid()+10000):
            self.assertEqual(self.call(PREFIX+'/api/accounts')[0],403)
        with patch('xbm.gateway.peer_uid',side_effect=OSError('not available')):
            self.assertEqual(self.call(PREFIX+'/')[0],403)

    def test_gateway_mutations_keep_one_time_dashboard_protection(self):
        self.assertEqual(self.call(PREFIX+'/api/storage/policy',body={'mode':'text','budget':0,'paused':True})[0],403)
        _,body,headers=self.call(PREFIX+'/api/dashboard-token')
        self.assertIsNone(headers.get('Access-Control-Allow-Origin'))
        proof=json.loads(body)['token']
        action={'X-XBM-Action':'storage','X-XBM-Dashboard':proof,'Origin':'https://nas.example'}
        self.assertEqual(self.call(PREFIX+'/api/storage/policy',body={'mode':'text','budget':0,'paused':True},headers=action)[0],200)
        self.assertEqual(self.call(PREFIX+'/api/storage/policy',body={'mode':'all','budget':0,'paused':False},headers=action)[0],403)

    def test_socket_collision_does_not_replace_files_or_live_server(self):
        existing=self.root/'existing'
        existing.write_text('keep')
        with self.assertRaises(ValueError):make_gateway(self.http,existing,allowed_uids={os.getuid()})
        with self.assertRaises(OSError):make_gateway(self.http,self.root/'app.sock',allowed_uids={os.getuid()})
        link=self.root/'link.sock';link.symlink_to(self.root/'app.sock')
        with self.assertRaises(ValueError):make_gateway(self.http,link,allowed_uids={os.getuid()})
        self.assertEqual(existing.read_text(),'keep')
