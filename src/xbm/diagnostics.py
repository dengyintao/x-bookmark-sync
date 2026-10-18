"""Allowlisted support report: never include paths, handles, content or secrets."""
import shutil
import tempfile
from . import __version__


def report(config):
    checks = []
    for label, path in [('database_storage', config.state_dir), ('media_storage', config.media_dir)]:
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=path) as probe:
                probe.write(b'xbm')
                probe.flush()
            free = shutil.disk_usage(path).free
            checks.append({'name': label, 'ok': free >= 512 * 1024**2,
                           'writable': True, 'free_bytes': free,
                           'message': '可写' if free >= 512 * 1024**2 else '空间不足 512 MB，请释放空间'})
        except OSError:
            checks.append({'name': label, 'ok': False, 'writable': False,
                           'message': '目录不可写或存储不可用，请检查应用权限和存储卷'})
    return {'ok': True, 'service': 'xbm', 'version': __version__, 'protocol_version': 1,
            'checks': checks, 'proxy_configured': bool(config.proxy.url),
            'authentication': 'paired_clients',
            'scope': '仅检查 NAS 存储；X 登录、采集完整性与扩展连接需在浏览器检查'}
