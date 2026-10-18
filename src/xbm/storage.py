"""Account-scoped permanent cleanup with a persistent, retryable file outbox."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import shutil
import threading
import time


class Storage:
    def __init__(self, accounts):
        self.accounts = accounts
        self._stop = threading.Event()
        self._thread = None

    @contextmanager
    def locked(self):
        with self.accounts._lock, ExitStack() as stack:
            libraries = list(self.accounts._libraries.values())
            for _, store in libraries:
                stack.enter_context(store._lock)
            yield libraries

    @staticmethod
    def safe_path(root, value):
        path = Path(value)
        resolved_root = root.resolve()
        if not path.is_absolute() or path.is_symlink() or path.resolve() == resolved_root or not path.resolve().is_relative_to(resolved_root):
            raise ValueError('清理路径不在此账户媒体目录内，请检查存储配置')
        return path

    @staticmethod
    def references(libraries, value):
        target = Path(value).resolve()
        for _, store in libraries:
            for row in store._conn.execute("SELECT local_path FROM media_files WHERE local_path!=''"):
                if Path(row[0]).resolve() == target:
                    return True
        return False

    def purge(self, account, ids):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(
                not isinstance(tid, str) or not tid.isascii() or not tid.isdigit() for tid in ids):
            raise ValueError('请选择回收站中的 1–100 条书签')
        cfg, store = self.accounts.get(account)
        marks = ','.join('?' for _ in ids)
        with self.locked():
            rows = store._conn.execute(f'SELECT tweet_id,deleted_at,purged_at FROM tweets WHERE tweet_id IN ({marks})', ids).fetchall()
            if len(rows) != len(set(ids)) or any(not row['deleted_at'] or row['purged_at'] for row in rows):
                raise ValueError('只能彻底清理回收站中的书签，请刷新页面')
            if store._conn.execute(f"SELECT 1 FROM media_files WHERE tweet_id IN ({marks}) AND status='downloading'",ids).fetchone():
                raise ValueError('选中书签仍有下载任务，请稍后再试')
            files = set()
            for row in store._conn.execute(f"SELECT local_path FROM media_files WHERE tweet_id IN ({marks}) AND local_path!=''", ids):
                path = self.safe_path(cfg.media_dir,row[0]);files.add(str(path))
            for tid in set(ids):
                directory = cfg.media_dir/tid
                if directory.is_symlink():
                    raise ValueError('书签媒体目录不可为符号链接')
                # Only files produced by our downloader; never recursively delete a user's directory.
                for candidate in directory.glob(tid+'_*'):
                    if candidate.is_file() and candidate.suffix.lower() in {'.jpg','.jpeg','.png','.webp','.gif','.mp4','.part'}:
                        files.add(str(self.safe_path(cfg.media_dir,candidate)))
                metadata = directory/'meta.json'
                if metadata.is_file():files.add(str(self.safe_path(cfg.media_dir,metadata)))
            with store._conn:
                for value in files:
                    store._conn.execute('INSERT OR IGNORE INTO purge_files(path) VALUES(?)',(value,))
                store._conn.execute(f'DELETE FROM media_files WHERE tweet_id IN ({marks})', ids)
                store._conn.execute(f"UPDATE tweets SET purged_at=?,author='',screen_name='',text='',created_at='',url='',has_article=0 WHERE tweet_id IN ({marks})",[time.time(),*ids])
        self.cleanup(account)
        return {'purged':len(rows), **self.summary(account)}

    def cleanup(self, account):
        cfg, store = self.accounts.get(account)
        # Lock all libraries while checking references and unlinking to avoid a check/use race.
        with self.locked() as libraries:
            rows = list(store._conn.execute('SELECT path FROM purge_files LIMIT 200'))
            for row in rows:
                try:
                    path = self.safe_path(cfg.media_dir,row[0])
                    if not self.references(libraries,path):
                        path.unlink(missing_ok=True)
                    with store._conn:
                        store._conn.execute('DELETE FROM purge_files WHERE path=?',(row[0],))
                    parent = path.parent
                    if parent != cfg.media_dir and parent.name.isascii() and parent.name.isdigit():
                        try: parent.rmdir()
                        except OSError: pass
                except (OSError, ValueError):
                    with store._conn:
                        store._conn.execute("UPDATE purge_files SET error='文件清理未完成，请检查权限或媒体目录' WHERE path=?",(row[0],))

    def summary(self, account):
        cfg, store = self.accounts.get(account)
        with store._lock:
            media = store._conn.execute('SELECT COALESCE(SUM(bytes),0) FROM media_files').fetchone()[0]
            trash = store._conn.execute('SELECT COUNT(*) FROM tweets WHERE deleted_at>0 AND purged_at=0').fetchone()[0]
            pending = store._conn.execute('SELECT COUNT(*) FROM purge_files').fetchone()[0]
        disk = shutil.disk_usage(cfg.media_dir if cfg.media_dir.exists() else cfg.data_root)
        return {'media_bytes':media, 'trash_count':trash, 'cleanup_pending':pending,
                'free_bytes':disk.free, 'total_bytes':disk.total}

    def start(self):
        def work():
            while not self._stop.wait(30):
                for row in self.accounts.list():
                    try:self.cleanup(row['id'])
                    except Exception:pass  # Persistent outbox retains failures for later retry.
        self._thread = threading.Thread(target=work,name='xbm-storage',daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:self._thread.join(timeout=35)
