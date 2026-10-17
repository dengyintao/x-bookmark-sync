"""Recoverable legacy ownership migration. No account or NAS path is hard-coded."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .accounts import account_key


@contextmanager
def libraries(manager, target):
    key = account_key(target)
    if not key:
        raise ValueError('请选择一个已识别的 X 账户')
    with manager._lock:
        source_cfg, source = manager.get('')
        target_cfg, dest = manager.get(key)
        # Store locks exclude ingestion, claims and progress updates. Active
        # downloads are rejected, not terminated halfway through writing a file.
        with source._lock, dest._lock:
            yield key, source_cfg, source, target_cfg, dest


def snapshot(source, dest):
    def read(store):
        return {table: [dict(r) for r in store._conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('tweets', 'media_files', 'sync_runs')}
    return read(source), read(dest)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def checked_file(row, root):
    path = Path(row['local_path']).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.stat().st_size != row['bytes']:
        raise ValueError('已完成媒体文件缺失、大小不符或路径不安全，迁移已停止')
    if not str(row['tweet_id']).isdigit() or path.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4'}:
        raise ValueError('媒体文件格式或书签 ID 不安全')
    return path


def plan_locked(key, source_cfg, source, target_cfg, dest):
    old, existing = snapshot(source, dest)
    busy = any(r['status'] == 'downloading' for data in (old, existing) for r in data['media_files'])
    errors = []
    if any(store._conn.execute('PRAGMA journal_mode').fetchone()[0] != 'delete' for store in (source,dest)):
        errors.append('当前数据库日志模式不支持原子迁移，请先停服离线检查')
    for data, cfg in [(old, source_cfg), (existing, target_cfg)]:
        for row in data['media_files']:
            if row['status'] == 'done':
                try:
                    checked_file(row, cfg.media_dir)
                except ValueError as error:
                    errors.append(str(error))
    target_ids = {r['tweet_id'] for r in existing['tweets']}
    target_urls = {(r['tweet_id'], r['remote_url']) for r in existing['media_files']}
    counts = {name: len(old[name]) for name in old}
    return {'account': key, **counts,
            'duplicate_tweets': sum(r['tweet_id'] in target_ids for r in old['tweets']),
            'duplicate_media': sum((r['tweet_id'], r['remote_url']) in target_urls for r in old['media_files']),
            'busy': busy, 'errors': errors[:5],
            'ready': bool(sum(counts.values())) and not busy and not errors,
            'token': digest([key, old, existing])}


def preview(manager, target):
    with libraries(manager, target) as args:
        return plan_locked(*args)


class Migrations:
    def __init__(self, manager):
        self.manager = manager
        self.path = manager.config.state_dir / 'operations.db'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._threads = []
        with self.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS migrations(
                id TEXT PRIMARY KEY, account TEXT, status TEXT, created_at REAL,
                updated_at REAL, result TEXT DEFAULT '{}', error TEXT DEFAULT '')''')
            conn.execute('CREATE TABLE IF NOT EXISTS browser_account(id INTEGER PRIMARY KEY CHECK(id=1), account TEXT, observed_at REAL)')
        self.recover()
        self.import_receipts()

    def connect(self):
        # A fresh connection per short journal operation avoids sharing a
        # connection between polling requests and a migration worker.
        return closing_connection(self.path)

    def update(self, job, status, result=None, error=''):
        with self.connect() as conn:
            conn.execute('UPDATE migrations SET status=?,updated_at=?,result=?,error=? WHERE id=?',
                         (status, time.time(), json.dumps(result or {}, ensure_ascii=False), error[:500], job))

    def list(self):
        with self.connect() as conn:
            rows = conn.execute('SELECT * FROM migrations ORDER BY created_at DESC LIMIT 30').fetchall()
        return [{**dict(r), 'result': json.loads(r['result'])} for r in rows]

    def get(self, job):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM migrations WHERE id=?', (job,)).fetchone()
        return {**dict(row), 'result': json.loads(row['result'])} if row else None

    def observe(self, value, observed_at=None):
        key = account_key(value)
        if not key:
            raise ValueError('无法识别登录账户')
        import math
        stamp = time.time() if observed_at is None else observed_at
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0 or stamp > time.time() + 300:
            raise ValueError('无效的账户识别时间')
        self.manager.get(key, create=True)
        with self.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO browser_account VALUES (1,?,?)', (key, stamp))
        return self.observed()

    def observed(self):
        with self.connect() as conn:
            row = conn.execute('SELECT account,observed_at FROM browser_account WHERE id=1').fetchone()
        return dict(row) if row else None

    def start(self, target, token, *, synchronous=False):
        key = account_key(target)
        if not key or not isinstance(token, str) or len(token) != 64:
            raise ValueError('请先预览并确认目标账户')
        self.manager.get(key)  # Never create a guessed account from a migration request.
        with self._lock, self.connect() as conn:
            if conn.execute("SELECT 1 FROM migrations WHERE status IN ('queued','running','rolling_back')").fetchone():
                raise ValueError('已有迁移正在执行，请等待完成')
            job = uuid.uuid4().hex
            now = time.time()
            conn.execute('INSERT INTO migrations(id,account,status,created_at,updated_at) VALUES (?,?,?,?,?)',
                         (job, key, 'queued', now, now))
        if synchronous:
            self.execute(job, key, token)
        else:
            thread = threading.Thread(target=self.execute, args=(job, key, token), daemon=True)
            self._threads.append(thread)
            thread.start()
        return job

    def execute(self, job, key, token):
        result = {}
        try:
            self.update(job, 'running')
            with libraries(self.manager, key) as args:
                _, source_cfg, source, target_cfg, dest = args
                plan = plan_locked(*args)
                if plan['token'] != token:
                    raise ValueError('数据在预览后发生变化，请重新预览')
                if not plan['ready']:
                    raise ValueError('未归属库为空、媒体校验失败或仍有下载进行中，请稍后重新预览')
                backup_root = (Path(self.manager.config.migration_backup_dir).expanduser()
                               if self.manager.config.migration_backup_dir else
                               Path.home() / '.local/share/xbm/migrations')
                backup = backup_root / job
                backup.mkdir(parents=True, mode=0o700, exist_ok=False)
                for store, name in [(source, 'source.db'), (dest, 'target.db')]:
                    with sqlite3.connect(backup / name) as saved:
                        store._conn.backup(saved)
                    saved.close()
                result = {'account': key, 'backup': str(backup), 'plan': plan, 'created_files': [], 'metadata': []}
                # This intent is durable before changing the two library databases.
                self.update(job, 'running', result)
                self.transfer(job, args, backup, result)
                self.update(job, 'completed', result)
        except Exception as error:
            # If the attached transaction committed but a receipt write failed,
            # the marker allows startup/retry to reconcile success without replay.
            status = 'completed' if self.committed(job, key) else 'failed'
            self.update(job, status, result, str(error))

    def transfer(self, job, args, backup, result):
        key, source_cfg, source, target_cfg, dest = args
        old, previous = snapshot(source, dest)
        conn = sqlite3.connect(target_cfg.state_dir / 'xbm.db')
        conn.row_factory = sqlite3.Row
        conn.execute('ATTACH DATABASE ? AS legacy', (str(source_cfg.state_dir / 'xbm.db'),))
        for db in ('main', 'legacy'):
            if conn.execute(f'PRAGMA {db}.journal_mode').fetchone()[0] != 'delete':
                conn.close()
                raise ValueError('迁移要求 SQLite DELETE 日志模式；请先离线检查数据库')
        conn.execute('CREATE TABLE IF NOT EXISTS ownership_migrations(id TEXT PRIMARY KEY)')
        conn.commit()
        committed = False
        try:
            conn.execute('BEGIN IMMEDIATE')
            for row in old['tweets']:
                insert(conn, 'tweets', row, ignore=True)
                # Preserve populated target fields, repair only missing metadata.
                for field in ('author', 'screen_name', 'text', 'created_at', 'url'):
                    if row.get(field):
                        conn.execute(f"UPDATE tweets SET {field}=? WHERE tweet_id=? AND ({field}='' OR {field} IS NULL)",
                                     (row[field], row['tweet_id']))
            linked = merged = 0
            for row in old['media_files']:
                match = conn.execute('SELECT * FROM media_files WHERE tweet_id=? AND remote_url=?',
                                     (row['tweet_id'], row['remote_url'])).fetchone()
                if match:
                    merged += 1
                    if match['status'] == 'done':
                        continue  # Already validated by the preview under store locks.
                value = {k: v for k, v in row.items() if k != 'id'}
                if row['status'] == 'done':
                    file = checked_file(row, source_cfg.media_dir)
                    directory = target_cfg.media_dir / row['tweet_id']
                    if not directory.resolve().is_relative_to(target_cfg.media_dir.resolve()):
                        raise ValueError('目标媒体路径不安全')
                    directory.mkdir(parents=True, exist_ok=True)
                    path = directory / f"{row['tweet_id']}_import_{job}_{row['id']}{file.suffix}"
                    # Journal the intended path first, so a crash can clean up safely.
                    result['created_files'].append(str(path))
                    self.update(job, 'running', result)
                    if file.stat().st_dev == directory.stat().st_dev:
                        os.link(file, path)
                    else:
                        if shutil.disk_usage(directory).free < file.stat().st_size + 16 * 1024 * 1024:
                            raise ValueError('目标媒体卷空间不足，已停止迁移')
                        shutil.copy2(file, path)
                    if path.stat().st_size != row['bytes']:
                        raise ValueError('迁移后文件大小校验失败')
                    value.update(local_path=str(path), total_bytes=row['bytes'], speed=0)
                    linked += 1
                else:
                    value['local_path'] = ''
                if not match:
                    insert(conn, 'media_files', value)
                elif value['status'] == 'done':
                    conn.execute("UPDATE media_files SET status='done',local_path=?,bytes=?,total_bytes=?,speed=0,error='',next_retry_at=0 WHERE id=?",
                                 (value['local_path'], value['bytes'], value['total_bytes'], match['id']))
            for row in old['sync_runs']:
                insert(conn, 'sync_runs', {k: v for k, v in row.items() if k != 'id'})
            # Metadata writes are prepared before the DB commit; previous versions
            # are backed up and restored if any file operation fails.
            for tweet in old['tweets']:
                tid = tweet['tweet_id']
                if not str(tid).isdigit():
                    raise ValueError('书签 ID 不安全')
                rows = [dict(r) for r in conn.execute('SELECT * FROM media_files WHERE tweet_id=?', (tid,))]
                if rows and all(r['status'] == 'done' for r in rows):
                    path = target_cfg.media_dir / tid / 'meta.json'
                    if not path.resolve().is_relative_to(target_cfg.media_dir.resolve()):
                        raise ValueError('目标元数据路径不安全')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    saved = backup / f'meta-{tid}.json'
                    exists = path.exists()
                    if exists:
                        shutil.copy2(path, saved)
                    result['metadata'].append({'path': str(path), 'saved': str(saved) if exists else ''})
                    self.update(job, 'running', result)
                    document = dict(conn.execute('SELECT * FROM tweets WHERE tweet_id=?', (tid,)).fetchone())
                    document.update(account=key, media=[{'type': r['kind'], 'local_path': r['local_path'],
                        'remote_url': r['remote_url'], 'bytes': r['bytes']} for r in rows])
                    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
            for table in ('tweets', 'media_files', 'sync_runs'):
                conn.execute(f'DELETE FROM legacy.{table}')
            expected = len({r['tweet_id'] for r in old['tweets'] + previous['tweets']})
            if conn.execute('SELECT COUNT(*) FROM tweets').fetchone()[0] != expected:
                raise ValueError('迁移后书签数量校验失败')
            result.update(tweets=len(old['tweets']), media=len(old['media_files']), runs=len(old['sync_runs']),
                          merged_media=merged, transferred_files=linked, total=expected, validated=True)
            after = []
            for db in ('legacy','main'):
                after.append({table:[dict(r) for r in conn.execute(f'SELECT * FROM {db}.{table} ORDER BY rowid')] for table in ('tweets','media_files','sync_runs')})
            result['after_token'] = digest([key, *after])
            self.update(job, 'running', result)
            conn.execute('INSERT INTO ownership_migrations(id) VALUES (?)', (job,))
            conn.commit()
            committed = True
            (backup / 'receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            if not committed:
                conn.rollback()
                self.cleanup(result, target_cfg, job)
            conn.close()

    def cleanup(self, result, cfg, job):
        for item in result.get('metadata', []):
            path = Path(item['path'])
            if path.resolve().is_relative_to(cfg.media_dir.resolve()) and path.name == 'meta.json':
                if item['saved']:
                    shutil.copy2(item['saved'], path)
                else:
                    path.unlink(missing_ok=True)
        for name in result.get('created_files', []):
            path = Path(name)
            if path.resolve().is_relative_to(cfg.media_dir.resolve()) and f'_import_{job}_' in path.name:
                path.unlink(missing_ok=True)

    def committed(self, job, key):
        try:
            cfg, _ = self.manager.get(key)
            with sqlite3.connect(cfg.state_dir / 'xbm.db') as conn:
                return bool(conn.execute('SELECT 1 FROM ownership_migrations WHERE id=?', (job,)).fetchone())
        except (sqlite3.Error, KeyError):
            return False

    def rollback(self, job):
        item = self.get(job)
        if not item or item['status'] != 'completed' or not item['result'].get('after_token'):
            raise ValueError('该任务没有可安全撤销的快照')
        result = item['result']
        with libraries(self.manager, item['account']) as args:
            key, source_cfg, source, target_cfg, dest = args
            if digest([key, *snapshot(source, dest)]) != result['after_token']:
                raise ValueError('归属后已有新数据或下载状态变化，为避免覆盖数据，不能自动撤销')
            backup = Path(result['backup'])
            with sqlite3.connect(backup / 'source.db') as saved:
                saved.row_factory = sqlite3.Row
                old = {t: [dict(r) for r in saved.execute(f'SELECT * FROM {t}')] for t in ('tweets','media_files','sync_runs')}
            with sqlite3.connect(backup / 'target.db') as saved:
                saved.row_factory = sqlite3.Row
                previous = {t: [dict(r) for r in saved.execute(f'SELECT * FROM {t}')] for t in ('tweets','media_files','sync_runs')}
            self.update(job, 'rolling_back', result)
            conn = sqlite3.connect(target_cfg.state_dir / 'xbm.db')
            conn.execute('ATTACH DATABASE ? AS legacy', (str(source_cfg.state_dir / 'xbm.db'),))
            try:
                conn.execute('BEGIN IMMEDIATE')
                for db, rows in [('main', previous), ('legacy', old)]:
                    for table, values in rows.items():
                        conn.execute(f'DELETE FROM {db}.{table}')
                        for row in values:
                            insert(conn, db+'.'+table, row)
                conn.execute('DELETE FROM ownership_migrations WHERE id=?',(job,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            self.cleanup(result, target_cfg, job)
            self.update(job, 'rolled_back', result)
        return {'id':job,'status':'rolled_back'}

    def import_receipts(self):
        # Older releases supplied an offline helper. Preserve its receipts as
        # history, without replaying the migration or assuming a user's account.
        for file in Path.home().glob('xbm-account-migration-*/receipt.json'):
            try:
                job = 'legacy_' + digest(str(file.resolve()))[:32]
                with self.connect() as conn:
                    if conn.execute('SELECT 1 FROM migrations WHERE id=?',(job,)).fetchone():
                        continue
                receipt = json.loads(file.read_text(encoding='utf-8'))
                key = account_key(receipt.get('account'))
                if not key or not (file.parent/'legacy.db').is_file() or not (file.parent/'target-before.db').is_file():
                    continue
                cfg, store = self.manager.get(key)
                counts = {new:receipt[old] for new,old in [('tweets','migrated_tweets'),('media','migrated_media'),('runs','migrated_runs')]}
                if not all(isinstance(n,int) and n>=0 for n in counts.values()):
                    continue
                result = {**counts,'account':key,'backup':str(file.parent),'imported':True,'validated':False}
                stamp = file.stat().st_mtime
                with self.connect() as conn:
                    conn.execute('INSERT INTO migrations VALUES (?,?,?,?,?,?,?)',
                        (job,key,'completed',stamp,stamp,json.dumps(result,ensure_ascii=False),''))
            except (OSError,ValueError,KeyError,TypeError):
                continue

    def recover(self):
        for item in self.list():
            if item['status'] == 'rolling_back':
                if self.committed(item['id'], item['account']):
                    self.update(item['id'], 'completed', item['result'], '撤销被重启中断，原归属仍然有效')
                else:
                    cfg, _ = self.manager.get(item['account'])
                    self.cleanup(item['result'], cfg, item['id'])
                    self.update(item['id'], 'rolled_back', item['result'])
            elif item['status'] in ('queued', 'running'):
                if self.committed(item['id'], item['account']):
                    self.update(item['id'], 'completed', item['result'])
                else:
                    try:
                        cfg, _ = self.manager.get(item['account'])
                        self.cleanup(item['result'], cfg, item['id'])
                    except (KeyError, ValueError, OSError):
                        pass  # Preserve backup and report interruption even if storage is unavailable.
                    self.update(item['id'], 'interrupted', item['result'], '服务重启中断了迁移，数据库事务已回滚，请重新预览')

    def close(self):
        for thread in self._threads:
            thread.join()


@contextmanager
def closing_connection(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def insert(conn, table, row, ignore=False):
    columns = list(row)
    conn.execute(f"INSERT {'OR IGNORE' if ignore else ''} INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", list(row.values()))
