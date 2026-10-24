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
        prefix = getattr(handler, 'gateway_prefix', '')
        if prefix and name == 'index.html':
            body = body.replace(b'="/', ('="' + prefix + '/').encode())
        elif prefix and name == 'manifest.webmanifest':
            import json
            manifest = json.loads(body)
            manifest['start_url'] = prefix + '/'
            manifest['scope'] = prefix + '/'
            for icon in manifest['icons']:
                icon['src'] = prefix + icon['src']
            body = json.dumps(manifest, ensure_ascii=False).encode()
        content_type = {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css',
                        'svg': 'image/svg+xml', 'webmanifest': 'application/manifest+json'}[name.rsplit('.', 1)[1]]
        handler.send_response(200)
        handler.send_header('Content-Type', content_type + '; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Cache-Control', 'no-store')
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
                status = query.get('status', [''])[0]
                if status not in {'', 'done', 'incomplete', 'failed', 'pending', 'downloading'}:
                    raise ValueError('invalid bookmark download status')
                result = handler.store.list_bookmarks(query.get('q', [''])[0][:300], kind, limit, offset,
                                                     status, query.get('trash', ['0'])[0] == '1')
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
        handler.send_header('Cache-Control', 'no-store')
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
    path = urlsplit(handler.path).path
    if path == '/api/storage/policy':
        import json
        if not same_origin(handler, 'storage'):
            handler._json(403, {'ok':False,'error':'请从已登录的存储管理页面操作'})
            return True
        try:
            length = int(handler.headers.get('Content-Length', 0))
            if not 0 < length <= 4096:raise ValueError('invalid body')
            data = json.loads(handler.rfile.read(length))
            if not isinstance(data,dict):raise ValueError('invalid body')
            handler.store.set_download_policy(data.get('mode'),data.get('budget'),data.get('paused'))
            handler._json(200, {'ok':True,'policy':handler.store.download_policy()})
        except (ValueError,TypeError) as error:
            handler._json(400, {'ok':False,'error':str(error)})
        return True
    if path in {'/api/bookmarks/delete', '/api/bookmarks/restore', '/api/bookmarks/purge'}:
        import json
        if not same_origin(handler, 'bookmarks'):
            handler._json(403, {'ok': False, 'error': '请刷新管理页面后重试；若通过代理访问，请打开 NAS 的直接地址操作', 'code':'dashboard_origin_required'})
            return True
        try:
            length = int(handler.headers.get('Content-Length', 0))
            if not 0 < length <= 8192:
                raise ValueError('invalid request body')
            data = json.loads(handler.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('invalid request body')
            if path.endswith('/purge'):
                result = handler.storage.purge(handler.account, data.get('ids'))
                handler._json(200, {'ok': True, **result})
            else:
                changed = handler.store.set_deleted(data.get('ids'), path.endswith('/restore'))
                handler._json(200, {'ok': True, 'changed': changed})
        except (ValueError, TypeError) as error:
            handler._json(400, {'ok': False, 'error': str(error)})
        return True
    match = re.fullmatch(r'/api/downloads/(\d+)/retry', urlsplit(handler.path).path)
    if not match:
        return False
    if not same_origin(handler, 'retry'):
        handler._json(403, {'ok': False, 'error': '请刷新管理页面后重试；若通过代理访问，请打开 NAS 的直接地址操作', 'code':'dashboard_origin_required'})
        return True
    changed = handler.store.retry_media(int(match[1]))
    handler._json(200 if changed else 409, {'ok': changed,
                  'message': '已加入重试队列' if changed else '任务不存在或已在下载/已完成'})
    return True


def management(handler, method):
    """Operations bypass library selection so journal polling stays responsive."""
    import json
    from .migration import preview
    parts = urlsplit(handler.path)
    rollback = re.fullmatch(r'/api/migrations/([a-z0-9_]+)/rollback', parts.path)
    if not rollback and parts.path not in {'/api/migrations', '/api/migrations/preview', '/api/browser-account'}:
        return False
    try:
        if method == 'GET':
            if rollback:
                raise ValueError('use POST for rollback')
            if parts.path == '/api/migrations':
                items = handler.migrations.list()
                result = {'items': items, 'total': len(items)}
            elif parts.path == '/api/browser-account':
                result = {'observed': handler.migrations.observed()}
            else:
                target = parse_qs(parts.query).get('target', [''])[0]
                result = preview(handler.accounts, target)
            handler._json(200, {'ok': True, **result})
        else:
            if parts.path == '/api/migrations/preview':
                raise ValueError('use GET for preview')
            if (parts.path == '/api/migrations' or rollback) and not same_origin(handler, 'migrate'):
                handler._json(403, {'ok': False, 'error': '请刷新管理页面后重试；若通过代理访问，请打开 NAS 的直接地址操作', 'code':'dashboard_origin_required'})
                return True
            length = int(handler.headers.get('Content-Length', 0))
            if not 0 < length <= 8192:
                raise ValueError('invalid operation body')
            payload = json.loads(handler.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('body must be an object')
            if rollback:
                result = handler.migrations.rollback(rollback[1])
            elif parts.path == '/api/browser-account':
                result = {'observed': handler.migrations.observe(payload.get('account'), payload.get('observed_at'))}
            else:
                job = handler.migrations.start(payload.get('account'), payload.get('token'))
                result = {'id': job}
            handler._json(202 if parts.path == '/api/migrations' else 200, {'ok': True, **result})
    except (ValueError, KeyError) as error:
        handler._json(400, {'ok': False, 'error': str(error)[:200]})
    except Exception:
        handler._json(500, {'ok': False, 'error': '操作失败，请检查 NAS 存储空间和服务日志'})
    return True


def same_origin(handler, action):
    """Browser Fetch Metadata survives proxies that rewrite the upstream Host.

    X-XBM-Action forces preflight for cross-origin JS; cross-site form requests
    cannot supply it. Never trust arbitrary Forwarded/X-Forwarded-Host headers.
    Non-browser tools without Origin still need the explicit action header.
    """
    if handler.headers.get('X-XBM-Action') != action:
        return False
    proof = handler.headers.get('X-XBM-Dashboard')
    if proof:
        if getattr(handler, '_dashboard_verified', False):return True
        valid = getattr(handler, 'dashboard', None) and handler.dashboard.consume(proof)
        handler._dashboard_verified = bool(valid)
        return bool(valid)
    site = handler.headers.get('Sec-Fetch-Site')
    if site == 'cross-site':
        return False
    origin = handler.headers.get('Origin')
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
            return False
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        expected = urlsplit(parsed.scheme + '://' + handler.headers.get('Host', ''))
        direct = parsed.hostname.lower() == (expected.hostname or '').lower() and port == (expected.port or (443 if parsed.scheme == 'https' else 80))
        return direct or site == 'same-origin'
    except ValueError:
        return False
