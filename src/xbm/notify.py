"""Failure alerts: Bark / Telegram Bot / log only.

Callers debounce (see ``fail_alert_threshold``); this module sends one message
and never raises. A broken notification channel must never stop syncing, and
messages must not contain cookies or other secrets.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any

TIMEOUT_SECONDS = 10
MAX_MESSAGE_CHARS = 1000
log = logging.getLogger('xbm.notify')


def _http_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ''
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return ''
    return value.strip()


def _post(url: str, payload: dict[str, Any]) -> int:
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.status


def alert(config: Any, message: str, title: str = 'X 书签同步失败') -> dict[str, Any]:
    """Send to every configured channel; return per-channel results."""
    message = str(message or '')[:MAX_MESSAGE_CHARS]
    log.warning('%s: %s', title, message)
    result: dict[str, Any] = {'log': True}
    settings = getattr(config, 'notify', None)
    if settings is None:
        return result

    bark = _http_url(getattr(settings, 'bark_url', ''))
    if bark:
        try:
            result['bark'] = _post(bark, {'title': title, 'body': message})
        except Exception as error:  # noqa: BLE001 - alerts are best effort
            result['bark'] = 'failed: ' + type(error).__name__
            log.warning('Bark alert failed: %s', error)

    token = str(getattr(settings, 'telegram_bot_token', '') or '')
    chat = str(getattr(settings, 'telegram_chat_id', '') or '')
    if token and chat:
        url = 'https://api.telegram.org/bot' + urllib.parse.quote(token, safe=':') + '/sendMessage'
        try:
            result['telegram'] = _post(url, {
                'chat_id': chat,
                'text': title + '\n' + message,
                'disable_web_page_preview': True,
            })
        except Exception as error:  # noqa: BLE001
            result['telegram'] = 'failed: ' + type(error).__name__
            log.warning('Telegram alert failed: %s', error)
    return result