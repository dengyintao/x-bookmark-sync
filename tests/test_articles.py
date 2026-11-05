"""Long-article parsing, archiving and dashboard access with synthetic data."""
import asyncio
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from xbm.articles import (article_from_tweet, extract_article, fetch_article,
                          render_markdown, save_article)
from xbm.config import Config
from xbm.server import make_server

BLOCKS = {
    'title': 'Synthetic article',
    'blocks': [
        {'type': 'header-one', 'text': 'Heading'},
        {'type': 'unstyled', 'text': 'Body paragraph'},
        {'type': 'unordered-list-item', 'text': 'Item'},
        {'type': 'blockquote', 'text': 'Quote'},
        {'type': '   ', 'text': '  '},
    ],
}


class ArticleParsing(unittest.TestCase):
    def test_plain_text_and_metadata_render_markdown(self):
        article = extract_article({'plain_text': 'Full body'})
        self.assertEqual(article['text'], 'Full body')
        markdown = render_markdown(article, author='作者', screen_name='handle',
                                   url='https://x.com/i/status/42', created_at='2026-01-02')
        self.assertIn('- 作者：作者 (@handle)', markdown)
        self.assertIn('- 原文：https://x.com/i/status/42', markdown)
        self.assertTrue(markdown.endswith('Full body\n'))

    def test_graphql_wrappings_and_block_types(self):
        wrapped = {'article': {'article_results': {'result': BLOCKS}}}
        article = extract_article(wrapped)
        self.assertEqual(article['title'], 'Synthetic article')
        markdown = render_markdown(article)
        self.assertIn('# Synthetic article', markdown)
        self.assertIn('# Heading', markdown)
        self.assertIn('Body paragraph', markdown)
        self.assertIn('- Item', markdown)
        self.assertIn('> Quote', markdown)
        self.assertNotIn('   ', markdown)

    def test_preview_text_alone_is_never_archived(self):
        self.assertIsNone(extract_article({'title': 'T', 'preview_text': 'truncated…'}))
        self.assertIsNone(article_from_tweet({'legacy': {'article': {'preview_text': 'x'}}}))
        self.assertIsNone(article_from_tweet({'legacy': {'full_text': 'plain tweet'}}))

    def test_article_from_tweet_supports_legacy_and_detail_shapes(self):
        legacy = {'legacy': {'article': {'title': 'L', 'plain_text': 'body'}}}
        self.assertEqual(article_from_tweet(legacy)['text'], 'body')
        detail = {'article': {'article_results': {'result': {'content_state': {'blocks': [
            {'type': 'unstyled', 'text': 'Detail body'}]}}}}}
        self.assertEqual(article_from_tweet(detail)['blocks'][0]['text'], 'Detail body')

    def test_render_rejects_empty_body_and_truncates_oversized(self):
        with self.assertRaises(ValueError):
            render_markdown({'title': 'no body'})
        markdown = render_markdown({'text': 'x' * 2_000_005})
        self.assertIn('[正文过长，已截断]', markdown)
        self.assertLess(len(markdown), 2_000_100)

    def test_save_article_is_atomic_and_path_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = save_article(tmp, '42', '# Title\n')
            self.assertEqual(path, Path(tmp) / '42' / 'article.md')
            self.assertEqual(path.read_text(encoding='utf-8'), '# Title\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            save_article(tmp, '42', '# Title v2\n')
            self.assertEqual(path.read_text(encoding='utf-8'), '# Title v2\n')
            for bad in ['../escape', '4/2', '', 'a']:
                with self.assertRaises(ValueError):
                    save_article(tmp, bad, 'x')
            with self.assertRaises(ValueError):
                save_article(tmp, '42', '   ')

    def test_fetch_article_never_invents_content(self):
        class Full:
            async def get_tweet_by_id(self, tweet_id):
                return SimpleNamespace(
                    _data={'article': {'plain_text': 'Body'}},
                    user=SimpleNamespace(name='N', screen_name='h'), created_at='c')

        class Empty:
            async def get_tweet_by_id(self, tweet_id):
                return SimpleNamespace(_data={})

        text = asyncio.run(fetch_article('7', client=Full()))
        self.assertIn('Body', text)
        self.assertIn('@h', text)
        self.assertIsNone(asyncio.run(fetch_article('7', client=Empty())))
        self.assertIsNone(asyncio.run(fetch_article('7')))
        with self.assertRaises(ValueError):
            asyncio.run(fetch_article('7/../escape', client=Full()))


class ArticleHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_root=Path(self.tmp.name))
        self.server = make_server(self.cfg, 0, require_auth=False)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server._xbm_migrations.close()
        self.server._xbm_accounts.close()
        self.tmp.cleanup()

    def ingest(self, payload):
        request = Request(self.base + '/api/ingest', data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request) as response:
            return json.load(response)

    def get(self, path):
        with urlopen(self.base + path) as response:
            return json.load(response)

    def mutate(self, action, ids):
        request = Request(self.base + '/api/bookmarks/' + action, method='POST',
                          data=json.dumps({'ids': ids}).encode(),
                          headers={'X-XBM-Action': 'bookmarks', 'Content-Type': 'application/json'})
        with urlopen(request) as response:
            return json.load(response)

    def test_article_is_archived_and_served_per_account(self):
        self.ingest({'tweets': [{'tweet_id': '42', 'text': 'teaser', 'has_article': True,
                                 'author': '作者', 'screen_name': 'handle', 'article': BLOCKS}]})
        item = self.get('/api/bookmarks')['items'][0]
        self.assertTrue(item['has_article'])
        self.assertTrue(item['article_saved'])
        self.assertEqual(item['article_title'], 'Synthetic article')
        article = self.get('/api/article/42')
        self.assertEqual(article['tweet_id'], '42')
        self.assertIn('# Synthetic article', article['markdown'])
        self.assertEqual(article['source'], 'extension')
        self.assertTrue((self.cfg.media_dir / '42' / 'article.md').is_file())
        with self.assertRaises(HTTPError) as error:
            self.get('/api/article/99')
        self.assertEqual(error.exception.code, 404)

    def test_metadata_only_article_stays_unsaved(self):
        self.ingest({'tweets': [{'tweet_id': '7', 'has_article': True}]})
        item = self.get('/api/bookmarks')['items'][0]
        self.assertTrue(item['has_article'])
        self.assertFalse(item['article_saved'])
        self.assertEqual(item['article_title'], '')
        with self.assertRaises(HTTPError):
            self.get('/api/article/7')

    def test_article_is_scoped_to_its_x_account(self):
        self.ingest({'account': 'x:first', 'tweets': [
            {'tweet_id': '42', 'article': BLOCKS}]})
        self.ingest({'account': 'x:second', 'tweets': [{'tweet_id': '43'}]})
        self.assertEqual(self.get('/api/article/42?account=x%3Afirst')['tweet_id'], '42')
        with self.assertRaises(HTTPError) as error:
            self.get('/api/article/42?account=x%3Asecond')
        self.assertEqual(error.exception.code, 404)
        with self.assertRaises(HTTPError) as error:
            self.get('/api/article/43?account=x%3Asecond')
        self.assertEqual(error.exception.code, 404)

    def test_invalid_article_rejects_the_batch_without_partial_write(self):
        for article in ['text', {'blocks': [{'type': 'unstyled', 'text': 5}]},
                        {'blocks': 'nope'}]:
            with self.assertRaises(HTTPError) as error:
                self.ingest({'tweets': [{'tweet_id': '77', 'article': article}]})
            self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.server._xbm_store.count_tweets(), 0)
        self.assertIsNone(self.server._xbm_store.get_article('77'))

    def test_permanent_purge_removes_article_and_resync_does_not_restore_it(self):
        self.ingest({'tweets': [{'tweet_id': '42', 'article': BLOCKS}]})
        self.mutate('delete', ['42'])
        self.mutate('purge', ['42'])
        self.assertIsNone(self.server._xbm_store.get_article('42'))
        self.assertFalse((self.cfg.media_dir / '42' / 'article.md').exists())
        self.ingest({'tweets': [{'tweet_id': '42', 'article': BLOCKS}]})
        self.assertIsNone(self.server._xbm_store.get_article('42'))
        self.assertFalse((self.cfg.media_dir / '42' / 'article.md').exists())


class ArticleCommand(unittest.TestCase):
    def test_fallback_command_saves_article_and_reports_missing(self):
        from xbm import cli
        from xbm.db import Store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'data'
            config = Path(tmp) / 'config.toml'
            config.write_text('[data]\nroot=' + json.dumps(str(root)))

            async def found(tweet_id, state_dir=None):
                return '# Read later\n\nBody\n'

            async def missing(tweet_id, state_dir=None):
                return None

            async def absent(tweet_id, state_dir=None):
                raise FileNotFoundError('cookies missing')

            seed = Store(root / '.xbm' / 'xbm.db')
            for tweet_id in ('42', '99', '100'):
                seed.record_tweet({'tweet_id': tweet_id, 'text': 'synthetic'})
            seed.close()

            args = SimpleNamespace(config=str(config), tweet_id='42')
            output = io.StringIO()
            with patch('xbm.cli.articles.fetch_article', side_effect=found), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(cli.cmd_article(args), 0)
            saved = Store(root / '.xbm' / 'xbm.db')
            article = saved.get_article('42')
            saved.close()
            self.assertEqual(article['title'], 'Read later')
            self.assertEqual(article['source'], 'twifork')
            self.assertEqual((root / 'media' / '42' / 'article.md').read_text(encoding='utf-8'),
                             '# Read later\n\nBody\n')

            with patch('xbm.cli.articles.fetch_article', side_effect=missing), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_article(SimpleNamespace(config=str(config), tweet_id='99')), 1)
            self.assertFalse((root / 'media' / '99').exists())

            with patch('xbm.cli.articles.fetch_article', side_effect=absent), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_article(SimpleNamespace(config=str(config), tweet_id='100')), 2)

            async def unexpected(tweet_id, state_dir=None):
                raise AssertionError('network must not be used for an unsynced tweet')

            with patch('xbm.cli.articles.fetch_article', side_effect=unexpected), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_article(SimpleNamespace(config=str(config), tweet_id='123')), 1)


if __name__ == '__main__':
    unittest.main()