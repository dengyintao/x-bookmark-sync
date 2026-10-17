"""NAS dashboard routes; assets are bundled and require no external services."""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ASSETS = Path(__file__).with_name('web')


def page_args(query: dict) -> tuple[int, int]:
    limit = int(query.get('limit', ['24'])[0])
    offset = int(query.get('offset', ['0'])[0])
    if not 1 <= limit <= 100 or not 0 <= offset <= 10_000_000:
        raise ValueError('invalid pagination')
    return limit, offset


def get(handler) -> bool:
    parts = urlsplit(handler.path)
    path, query = parts.path, parse_qs(parts.query)
    if path in {'/', '/app.js', '/style.css', '/icon.svg', '/manifest.webmanifest'}:
        name = 'index.html' if path == '/' else path[1:]
        body = (ASSETS / name).read_bytes()
        content_type = {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css',
                        'svg': 'image/svg+xml', 'webmanifest': 'application/manifest+json'}[name.rsplit('.', 1)[1]]
        handler.send_response(200)
        handler.send_header('Content-Type', content_type + '; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
        handler.end_headers()
        handler.wfile.write(body)
        return True
    if path in {'/api/bookmarks', '/api/runs', '/api/downloads'}:
        try:
            limit, offset = page_args(query)
            if path == '/api/bookmarks':
                kind = query.get('kind', [''])[0]
                if kind not in {'', 'photo', 'video', 'gif', 'hls', 'text'}:
                    raise ValueError('invalid media kind')
                result = handler.store.list_bookmarks(query.get('q', [''])[0][:300], kind, limit, offset)
            elif path == '/api/runs':
                result = handler.store.list_runs(limit, offset)
            else:
                status = query.get('status', [''])[0]
                if status not in {'', 'pending', 'downloading', 'done', 'failed'}:
                    raise ValueError('invalid download status')
                result = handler.store.list_downloads(status, limit, offset)
            handler._json(200, {'ok': True, **result})
        except ValueError as error:
            handler._json(400, {'ok': False, 'error': str(error)})
        return True
    match = re.fullmatch(r'/api/media/(\d+)', path)
    if match:
        serve_media(handler, int(match[1]))
        return True
    return False


def serve_media(handler, media_id: int) -> None:
    media = handler.store.get_media(media_id)
    if not media or media['status'] != 'done' or not media['local_path']:
        handler._json(404, {'ok': False, 'error': 'media is not downloaded'})
        return
    path = Path(media['local_path']).resolve()
    if not path.is_relative_to(handler.config.media_dir.resolve()) or not path.is_file():
        handler._json(404, {'ok': False, 'error': 'media file unavailable'})
        return
    # Restrict served types even if an old database contains arbitrary filenames.
    if path.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4'}:
        handler._json(404, {'ok': False, 'error': 'unsupported media type'})
        return
    size = path.stat().st_size
    start, end, code = 0, size - 1, 200
    requested = handler.headers.get('Range')
    if requested:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
        try:
            if not match or not any(match.groups()):
                raise ValueError()
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), size - 1) if match[2] else size - 1
            else:
                suffix = int(match[2])
                if suffix <= 0:
                    raise ValueError()
                start = max(0, size - suffix)
            if start > end or start >= size:
                raise ValueError()
            code = 206
        except ValueError:
            handler.send_response(416)
            handler.send_header('Content-Range', f'bytes */{size}')
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return
    with path.open('rb') as file:
        handler.send_response(code)
        handler.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        handler.send_header('Content-Length', str(max(0, end - start + 1)))
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        if code == 206:
            handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        handler.end_headers()
        file.seek(start)
        remaining = end - start + 1
        try:
            while remaining > 0:
                chunk = file.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def post(handler) -> bool:
    match = re.fullmatch(r'/api/downloads/(\d+)/retry', urlsplit(handler.path).path)
    if not match:
        return False
    # Mutation is available only from our same-origin dashboard, not cross-origin forms.
    origin = handler.headers.get('Origin')
    host = handler.headers.get('Host')
    expected = urlsplit(origin) if origin else None
    if (handler.headers.get('X-XBM-Action') != 'retry' or
            (expected and (expected.netloc != host or expected.scheme not in {'http', 'https'})) or
            handler.headers.get('Sec-Fetch-Site') == 'cross-site'):
        handler._json(403, {'ok': False, 'error': 'same-origin dashboard required'})
        return True
    changed = handler.store.retry_media(int(match[1]))
    handler._json(200 if changed else 409, {'ok': changed,
                  'message': '已加入重试队列' if changed else '任务不存在或已在下载/已完成'})
    return True
