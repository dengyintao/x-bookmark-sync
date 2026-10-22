"""Local-owner bootstrap, hashed client credentials and short-lived pairing codes."""
from contextlib import contextmanager, closing
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time

SESSION_SECONDS = 30 * 24 * 3600
PAIR_SECONDS = 300


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Access:
    def __init__(self, state_dir):
        self.directory = Path(state_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.key_path = self.directory/'admin-key.txt'
        if self.key_path.is_symlink():
            raise ValueError('admin key must not be a symlink')
        try:
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w') as file:
                file.write(secrets.token_urlsafe(32)+'\n')
        self.key_path.chmod(0o600)
        if len(self.key_path.read_text().strip()) < 32:
            raise ValueError('admin key is invalid; restore it or reset access locally')
        self.path = self.directory/'access.db'
        if self.path.is_symlink():
            raise ValueError('access database must not be a symlink')
        self.lock = threading.RLock()
        self.failures = {}
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS clients(
                id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL, role TEXT NOT NULL,
                name TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pairing(hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), require_login INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS connection_requests(
                    id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, name TEXT NOT NULL,
                    expires REAL NOT NULL, approved INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS owner_password(id INTEGER PRIMARY KEY CHECK(id=1), salt BLOB NOT NULL, hash BLOB NOT NULL);''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        with self.lock, closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def master_matches(self, token):
        return isinstance(token, str) and len(token) <= 256 and hmac.compare_digest(
            digest(token), digest(self.key_path.read_text().strip()))

    def authenticate(self, token):
        if not isinstance(token, str) or not token or len(token) > 256:
            return None
        if self.master_matches(token):
            return {'id': 'owner', 'role': 'admin'}
        with self.db() as db:
            row = db.execute('SELECT id,role FROM clients WHERE hash=? AND expires>?',
                             (digest(token), time.time())).fetchone()
            return dict(row) if row else None

    def attempt(self, peer):
        # Limit guessing before checking either secret, including successful attempts.
        now = time.monotonic()
        with self.lock:
            self.failures = {k: v for k, v in self.failures.items() if v[1] > now}
            count, until = self.failures.get(peer, (0, now+60))
            if count >= 10 or len(self.failures) >= 1024 and peer not in self.failures:
                raise PermissionError('尝试过于频繁，请一分钟后重试')
            self.failures[peer] = (count+1, until)

    def _issue(self, db, role, name):
        token, client_id = secrets.token_urlsafe(32), secrets.token_hex(12)
        now = time.time()
        expires = now + (SESSION_SECONDS if role == 'admin' else 365 * 24 * 3600)
        db.execute('DELETE FROM clients WHERE expires<=?', (now,))
        db.execute('INSERT INTO clients VALUES(?,?,?,?,?,?)',
                   (client_id, digest(token), role, name[:80], now, expires))
        return {'token': token, 'id': client_id, 'expires': expires}

    def login(self, key, peer):
        self.attempt(peer)
        if not self.master_matches(key) and not self.password_matches(key):
            raise ValueError('管理密码或密钥不正确')
        with self.db() as db:
            return self._issue(db, 'admin', '管理页面')

    def configure_password(self, password):
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError('管理密码须为 12–256 个字符')
        with self.db() as db:
            if db.execute('SELECT 1 FROM owner_password WHERE id=1').fetchone():
                return False  # Replayed install/upgrade environments never overwrite a password.
            salt = secrets.token_bytes(16)
            hashed = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
            db.execute('INSERT INTO owner_password VALUES(1,?,?)', (salt, hashed))
        return True

    def password_matches(self, password):
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            return False
        with self.db() as db:
            row = db.execute('SELECT salt,hash FROM owner_password WHERE id=1').fetchone()
        if not row:
            return False
        hashed = hashlib.scrypt(password.encode(), salt=row['salt'], n=16384, r=8, p=1)
        return hmac.compare_digest(hashed, row['hash'])

    def security(self):
        with self.db() as db:
            has_password = bool(db.execute('SELECT 1 FROM owner_password').fetchone())
            row = db.execute('SELECT require_login FROM settings WHERE id=1').fetchone()
        return {'require_login': bool(row[0]) if row else has_password,
                'has_password': has_password}

    def set_security(self, required, password=None):
        if not isinstance(required, bool):
            raise ValueError('请选择是否启用管理密码')
        if required and not self.security()['has_password']:
            self.configure_password(password)
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(1,?)', (int(required),))
            if required:
                db.execute("DELETE FROM clients WHERE role='admin'")

    def local_session(self):
        with self.db() as db:
            if self.security()['require_login']:
                raise ValueError('请登录管理页面')
            return self._issue(db, 'admin', '局域网管理页面')

    def request_connection(self, name, peer):
        self.attempt(peer)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('浏览器名称须为 1–80 个字符')
        request_id, secret = secrets.token_hex(16), secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM connection_requests WHERE expires<=?', (time.time(),))
            if db.execute('SELECT count(*) FROM connection_requests').fetchone()[0] >= 128:
                raise PermissionError('连接请求过多，请稍后再试')
            db.execute('INSERT INTO connection_requests VALUES(?,?,?,?,0)',
                       (request_id, digest(secret), name.strip(), time.time()+PAIR_SECONDS))
        return {'id': request_id, 'secret': secret, 'expires_in': PAIR_SECONDS}

    def connection_request(self, request_id):
        if not isinstance(request_id, str) or len(request_id) > 64:
            raise ValueError('连接请求无效')
        with self.db() as db:
            row = db.execute('SELECT id,name,expires,approved FROM connection_requests WHERE id=? AND expires>?',
                             (request_id, time.time())).fetchone()
        if not row:
            raise ValueError('连接请求已过期，请回到扩展重试')
        return dict(row)

    def approve_connection(self, request_id):
        self.connection_request(request_id)
        with self.db() as db:
            db.execute('UPDATE connection_requests SET approved=1 WHERE id=? AND expires>?',
                       (request_id, time.time()))

    def claim_connection(self, request_id, secret):
        if not isinstance(request_id, str) or not isinstance(secret, str) or len(secret) > 128:
            raise ValueError('连接请求无效')
        with self.db() as db:
            row = db.execute('SELECT * FROM connection_requests WHERE id=? AND secret_hash=? AND expires>?',
                             (request_id, digest(secret), time.time())).fetchone()
            if not row:
                raise ValueError('连接请求已过期或已完成，请回到扩展重试')
            if not row['approved']:
                return {'pending': True}
            db.execute('DELETE FROM connection_requests WHERE id=?', (request_id,))
            return {'pending': False, **self._issue(db, 'extension', row['name'])}

    def pairing_code(self):
        code = secrets.token_urlsafe(12)
        with self.db() as db:
            db.execute('DELETE FROM pairing')  # Creating a new code invalidates the old one.
            db.execute('INSERT INTO pairing VALUES(?,?)', (digest(code), time.time()+PAIR_SECONDS))
        return {'code': code, 'expires_in': PAIR_SECONDS}

    def pair(self, code, name, peer):
        self.attempt(peer)
        if not isinstance(code, str) or len(code) > 128 or not isinstance(name, str):
            raise ValueError('配对信息无效')
        with self.db() as db:
            row = db.execute('SELECT hash FROM pairing WHERE hash=? AND expires>?',
                             (digest(code), time.time())).fetchone()
            if not row:
                raise ValueError('配对码无效或已过期，请在 NAS 页面重新生成')
            db.execute('DELETE FROM pairing WHERE hash=?', (row['hash'],))
            return self._issue(db, 'extension', name.strip() or '浏览器扩展')

    def clients(self):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                'SELECT id,name,created,expires FROM clients WHERE role=? AND expires>? ORDER BY created DESC',
                ('extension', time.time()))]

    def revoke(self, client_id):
        if not isinstance(client_id, str):
            raise ValueError('invalid client id')
        with self.db() as db:
            db.execute('DELETE FROM clients WHERE id=?', (client_id,))

    def reset(self):
        # Local CLI only: revoke all clients and replace the owner key atomically.
        key = secrets.token_urlsafe(32)
        temporary = self.directory/('admin-key-'+secrets.token_hex(8)+'.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write(key+'\n')
        with self.db() as db:
            db.execute('DELETE FROM clients')
            db.execute('DELETE FROM pairing')
            db.execute('DELETE FROM owner_password')
            db.execute('DELETE FROM settings')
            db.execute('DELETE FROM connection_requests')
            temporary.replace(self.key_path)
