"""fnOS session gateway: trust identity only on the nginx Unix-socket transport."""
import errno
import os
from pathlib import Path
import pwd
import re
import socket
import socketserver
import stat
import struct
from urllib.parse import urlsplit

PREFIX = '/app/x-bookmark-sync'


def peer_uid(connection):
    if not hasattr(socket, 'SO_PEERCRED'):
        raise OSError('fnOS gateway requires Linux peer credentials')
    return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def make_gateway(http_server, path: Path, *, allowed_uids=None):
    if allowed_uids is None:
        # fnOS nginx workers run as www-data. Never trust the same headers on TCP.
        allowed_uids = {0, pwd.getpwnam('www-data').pw_uid}
    path = Path(path)
    if path.is_symlink():
        raise ValueError('gateway socket must not be a symlink')
    if path.exists():
        if not stat.S_ISSOCK(path.stat().st_mode):
            raise ValueError('gateway path is not a socket')
        with socket.socket(socket.AF_UNIX) as probe:
            try:
                probe.connect(str(path))
            except OSError as error:
                if error.errno != errno.ECONNREFUSED:
                    raise
            else:
                raise OSError('gateway socket is already in use')
        path.unlink()
    base = http_server.RequestHandlerClass

    class GatewayHandler(base):
        gateway_prefix = PREFIX

        def _access_gate(self):
            try:
                trusted = peer_uid(self.request) in allowed_uids
            except OSError:
                trusted = False
            uid = self.headers.get('X-Trim-Userid', '')
            if not trusted or self.headers.get('X-Trim-Isadmin') != 'true' or not re.fullmatch(r'[0-9]{1,10}', uid):
                self._json(403, {'ok': False, 'error': '请使用 NAS 管理员登录飞牛后打开应用'})
                return False
            parts = urlsplit(self.path)
            if parts.path != PREFIX and not parts.path.startswith(PREFIX + '/'):
                self._json(404, {'ok': False, 'error': 'not found'})
                return False
            self.path = (parts.path[len(PREFIX):] or '/') + ('?' + parts.query if parts.query else '')
            self.gateway_user = {'id': 'nas:' + uid, 'role': 'admin'}
            return super()._access_gate()

    class GatewayServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

        def server_close(self):
            super().server_close()
            if path.exists() and not path.is_symlink() and path.stat().st_ino == getattr(self, 'socket_inode', None):
                path.unlink()

    server = GatewayServer(str(path), GatewayHandler)
    server.socket_inode = path.stat().st_ino
    # Different system users need to connect. SO_PEERCRED above authenticates the
    # OS peer before any assets, identity headers or private APIs are accepted.
    path.chmod(0o666)
    return server
