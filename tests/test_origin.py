import unittest
from types import SimpleNamespace
from xbm.web import same_origin

class OriginTests(unittest.TestCase):
    def test_proxy_browser_evidence_and_cross_site_rejection(self):
        def check(**headers):
            return same_origin(SimpleNamespace(headers={'X-XBM-Action':'bookmarks',**headers}),'bookmarks')
        self.assertTrue(check(Origin='http://nas.example',Host='nas.example:80'))
        self.assertTrue(check(Origin='https://nas.example',Host='127.0.0.1:8765',**{'Sec-Fetch-Site':'same-origin'}))
        self.assertFalse(check(Origin='https://evil.example',Host='nas.example',**{'Sec-Fetch-Site':'cross-site'}))
        self.assertFalse(check(Origin='https://evil.example',Host='nas.example',**{'X-Forwarded-Host':'evil.example'}))
        self.assertFalse(check(Origin='null',Host='nas.example',**{'Sec-Fetch-Site':'same-origin'}))

    def test_one_use_intent_token_has_no_cors_and_survives_changed_headers(self):
        import json,threading,tempfile
        from pathlib import Path
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        from xbm.config import Config
        from xbm.server import make_server,ingest_payload
        with tempfile.TemporaryDirectory() as tmp:
            server=make_server(Config(data_root=Path(tmp)),0)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                ingest_payload({'tweets':[{'tweet_id':'123','text':'synthetic'}]},server._xbm_store)
                with urlopen(base+'/api/dashboard-token') as response:
                    self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))
                    token=json.load(response)['token']
                headers={'X-XBM-Action':'bookmarks','X-XBM-Dashboard':token,'Origin':'null','Sec-Fetch-Site':'cross-site'}
                with urlopen(Request(base+'/api/bookmarks/delete',data=b'{"ids":["123"]}',headers=headers)) as response:self.assertTrue(json.load(response)['ok'])
                with self.assertRaises(HTTPError) as error:urlopen(Request(base+'/api/bookmarks/restore',data=b'{"ids":["123"]}',headers=headers))
                self.assertEqual(error.exception.code,403)
            finally:
                server.shutdown();server.server_close();server._xbm_accounts.close();server._xbm_migrations.close()
