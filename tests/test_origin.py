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
