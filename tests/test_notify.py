"""Alert channels are best effort and never break the sync path."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from xbm import notify
from xbm.config import Config, NotifyConfig


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=None):
        return b'{}'


def config(**notify_settings):
    return Config(data_root=Path('/tmp/xbm-notify-test'),
                  notify=NotifyConfig(**notify_settings))


class NotifyTest(unittest.TestCase):
    def test_log_only_when_no_channel_is_configured(self):
        with patch('xbm.notify.urllib.request.urlopen') as urlopen, \
                self.assertLogs('xbm.notify', level='WARNING') as logs:
            result = notify.alert(config(), 'boom')
        self.assertEqual(result, {'log': True})
        self.assertIn('boom', logs.output[0])
        urlopen.assert_not_called()

    def test_bark_and_telegram_receive_the_expected_payloads(self):
        settings = config(bark_url='https://api.day.app/SYNTHETIC',
                          telegram_bot_token='123:abc', telegram_chat_id='9')
        with patch('xbm.notify.urllib.request.urlopen', return_value=FakeResponse(200)) as urlopen, \
                self.assertLogs('xbm.notify', level='WARNING'):
            result = notify.alert(settings, 'sync failed')
        self.assertEqual(result['bark'], 200)
        self.assertEqual(result['telegram'], 200)
        bark, telegram = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(bark.full_url, 'https://api.day.app/SYNTHETIC')
        self.assertEqual(json.loads(bark.data.decode())['title'], 'X 书签同步失败')
        self.assertIn('sync failed', json.loads(bark.data.decode())['body'])
        self.assertEqual(telegram.full_url, 'https://api.telegram.org/bot123:abc/sendMessage')
        payload = json.loads(telegram.data.decode())
        self.assertEqual(payload['chat_id'], '9')
        self.assertIn('sync failed', payload['text'])

    def test_one_broken_channel_never_blocks_the_other_or_raises(self):
        settings = config(bark_url='https://api.day.app/SYNTHETIC',
                          telegram_bot_token='123:abc', telegram_chat_id='9')
        responses = [OSError('offline'), FakeResponse(200)]

        def urlopen(request, timeout=None):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with patch('xbm.notify.urllib.request.urlopen', side_effect=urlopen), \
                self.assertLogs('xbm.notify', level='WARNING'):
            result = notify.alert(settings, 'boom')
        self.assertTrue(result['bark'].startswith('failed: OSError'))
        self.assertEqual(result['telegram'], 200)

    def test_invalid_or_incomplete_channel_configuration_is_skipped(self):
        settings = config(bark_url='file:///etc/passwd', telegram_bot_token='123:abc')
        with patch('xbm.notify.urllib.request.urlopen') as urlopen, \
                self.assertLogs('xbm.notify', level='WARNING'):
            result = notify.alert(settings, 'boom')
        self.assertEqual(result, {'log': True})
        urlopen.assert_not_called()

    def test_long_messages_are_truncated(self):
        captured = {}

        def urlopen(request, timeout=None):
            captured['body'] = json.loads(request.data.decode())
            return FakeResponse(200)

        with patch('xbm.notify.urllib.request.urlopen', side_effect=urlopen), \
                self.assertLogs('xbm.notify', level='WARNING'):
            notify.alert(config(bark_url='https://api.day.app/x'), 'e' * 5000)
        self.assertEqual(len(captured['body']['body']), notify.MAX_MESSAGE_CHARS)


if __name__ == '__main__':
    unittest.main()