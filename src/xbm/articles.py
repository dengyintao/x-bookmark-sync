"""Long-form X Article payloads: normalization, Markdown rendering and files.

The bookmark timeline only carries article metadata; full text is present only
when X includes it in the response or when an authenticated fallback client
returns it. This module never promotes ``preview_text`` (a truncated teaser) to
an archived article: callers must show a missing body as missing.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

MAX_ARTICLE_CHARS = 2_000_000
_TWEET_ID = re.compile(r'[0-9]{1,30}')
_HEADINGS = {
    'header-one': '#',
    'header-1': '#',
    'header-two': '##',
    'header-2': '##',
    'header-three': '###',
    'header-3': '###',
}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ''


def _unwrap(value: Any) -> dict[str, Any] | None:
    """Accept the article wrappings used by X GraphQL responses."""
    if not isinstance(value, dict):
        return None
    if isinstance(value.get('article'), dict):
        return _unwrap(value['article'])
    results = value.get('article_results')
    if isinstance(results, dict):
        return _unwrap(results.get('result') or results)
    result = value.get('result')
    if isinstance(result, dict):
        return _unwrap(result)
    return value


def _article_blocks(node: dict[str, Any]) -> list[dict[str, str]]:
    state = node.get('content_state')
    if not isinstance(state, dict):
        state = node.get('content') if isinstance(node.get('content'), dict) else {}
    raw = state.get('blocks') if isinstance(state, dict) else None
    if not isinstance(raw, list):
        raw = node.get('blocks') if isinstance(node.get('blocks'), list) else []
    blocks = []
    for block in raw:
        if not isinstance(block, dict):
            continue
        text = _text(block.get('text'))
        if not text.strip():
            continue
        blocks.append({'type': _text(block.get('type')) or 'unstyled', 'text': text})
    return blocks


def extract_article(value: Any) -> dict[str, Any] | None:
    """Return ``{title, text, blocks}`` only when a full body is available."""
    node = _unwrap(value)
    if node is None:
        return None
    blocks = _article_blocks(node)
    text = _text(node.get('plain_text')) or _text(node.get('text'))
    if not text.strip() and not blocks:
        return None
    return {'title': _text(node.get('title')).strip(), 'text': text, 'blocks': blocks}


def article_from_tweet(value: Any) -> dict[str, Any] | None:
    """Extract an article from a tweet result (timeline or TweetDetail)."""
    if not isinstance(value, dict):
        return None
    candidates = [value.get('article')]
    legacy = value.get('legacy')
    if isinstance(legacy, dict):
        candidates.append(legacy.get('article'))
    for candidate in candidates:
        found = extract_article(candidate)
        if found:
            return found
    return None


def _blocks_markdown(blocks: list[dict[str, str]] | None) -> str:
    parts = []
    for block in blocks or []:
        text = _text(block.get('text')).strip()
        if not text:
            continue
        kind = _text(block.get('type')).lower()
        prefix = _HEADINGS.get(kind)
        if prefix:
            text = prefix + ' ' + text
        elif kind == 'blockquote':
            text = '> ' + text
        elif kind == 'unordered-list-item':
            text = '- ' + text
        elif kind == 'ordered-list-item':
            text = '1. ' + text
        parts.append(text)
    return '\n\n'.join(parts)


def render_markdown(article: dict[str, Any], *, author: str = '', screen_name: str = '',
                    url: str = '', created_at: str = '') -> str:
    """Render title, provenance and body; raise when there is no body."""
    title = _text(article.get('title')).strip()
    body = _blocks_markdown(article.get('blocks')) or _text(article.get('text')).strip()
    if not body:
        raise ValueError('article body is empty')
    if len(body) > MAX_ARTICLE_CHARS:
        body = body[:MAX_ARTICLE_CHARS] + '\n\n[正文过长，已截断]'
    lines = []
    if title:
        lines.extend([f'# {title}', ''])
    meta = []
    who, handle = _text(author).strip(), _text(screen_name).strip()
    if who or handle:
        meta.append('- 作者：' + who + (f' (@{handle})' if handle else ''))
    if _text(created_at).strip():
        meta.append('- 发布时间：' + created_at.strip())
    if _text(url).strip():
        meta.append('- 原文：' + url.strip())
    if meta:
        lines.extend(meta + [''])
    lines.append(body)
    return '\n'.join(lines).rstrip() + '\n'


def article_path(media_dir: str | Path, tweet_id: str) -> Path:
    if not isinstance(tweet_id, str) or not _TWEET_ID.fullmatch(tweet_id):
        raise ValueError('invalid tweet id')
    return Path(media_dir) / tweet_id / 'article.md'


def save_article(media_dir: str | Path, tweet_id: str, markdown: str) -> Path:
    """Atomically mirror the database copy into ``<media>/<tweet_id>/article.md``."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError('article markdown is empty')
    root = Path(media_dir)
    path = article_path(root, tweet_id)
    directory = path.parent
    if directory.is_symlink():
        raise ValueError('article directory must not be a symlink')
    directory.mkdir(parents=True, exist_ok=True)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('article path outside media directory')
    fd, temporary = tempfile.mkstemp(prefix='.article-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(markdown)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def _as_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    data = getattr(value, '_data', None)
    return data if isinstance(data, dict) else None


async def fetch_article(tweet_id: str, *, state_dir: str | Path | None = None,
                        client: Any = None) -> str | None:
    """Best-effort fetch through an authenticated twifork client.

    ``client`` may be supplied by tests or a fallback caller. When neither a
    client nor a logged-in ``state_dir`` is available, or the response carries
    no full body, return ``None`` rather than fabricating an article.
    """
    if not isinstance(tweet_id, str) or not _TWEET_ID.fullmatch(tweet_id):
        raise ValueError('invalid tweet id')
    if client is None:
        if state_dir is None:
            return None
        from .auth import make_client
        client = make_client(Path(state_dir))
    tweet = await client.get_tweet_by_id(tweet_id)
    article = article_from_tweet(_as_mapping(tweet) or {})
    if not article:
        article = extract_article(getattr(tweet, 'article', None))
    if not article:
        return None
    user = getattr(tweet, 'user', None)
    return render_markdown(
        article,
        author=_text(getattr(user, 'name', '')),
        screen_name=_text(getattr(user, 'screen_name', '')),
        url=f'https://x.com/i/status/{tweet_id}',
        created_at=_text(str(getattr(tweet, 'created_at', '') or '')),
    )