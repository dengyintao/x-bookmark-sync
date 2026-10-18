"""Private, verified metadata snapshots; media stays on its existing storage volume."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from .config import load
from . import __version__


def create_snapshot(config_path: Path, destination: Path):
    config_path, destination = Path(config_path), Path(destination)
    cfg = load(config_path)
    if not config_path.is_file():
        raise ValueError('configuration does not exist')
    if destination.exists():
        raise ValueError('backup destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.xbm-backup-', dir=destination.parent))
    try:
        os.chmod(staging, 0o700)
        shutil.copyfile(config_path, staging/'config.toml')
        os.chmod(staging/'config.toml', 0o600)
        paths = [cfg.state_dir/'xbm.db', cfg.state_dir/'operations.db']
        paths += sorted((cfg.data_root/'accounts').glob('*/.xbm/xbm.db'))
        files = []
        for path in paths:
            if not path.exists():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(cfg.data_root.resolve()):
                raise ValueError('database outside data root')
            target = staging/'databases'/path.relative_to(cfg.data_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True) as source:
                with sqlite3.connect(target) as output:
                    source.backup(output)
                    if output.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('database integrity check failed')
            os.chmod(target, 0o600)
            files.append(target)
        files.append(staging/'config.toml')
        manifest = {'version': __version__, 'format': 1, 'media_included': False,
                    'files': {str(p.relative_to(staging)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
        (staging/'manifest.json').write_text(json.dumps(manifest, indent=2))
        # Publish only a complete, verified snapshot.
        staging.rename(destination)
        return manifest
    except Exception:
        shutil.rmtree(staging)
        raise


def verify_snapshot(destination: Path):
    destination = Path(destination)
    manifest = json.loads((destination/'manifest.json').read_text())
    if manifest.get('format') != 1 or 'config.toml' not in manifest.get('files', {}):
        raise ValueError('invalid snapshot manifest')
    for name, digest in manifest['files'].items():
        path = destination/name
        if not path.resolve().is_relative_to(destination.resolve()) or path.is_symlink():
            raise ValueError('invalid snapshot path')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('snapshot checksum mismatch')
        if path.suffix == '.db':
            with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('snapshot database is corrupt')
    return manifest


def restore_snapshot(source: Path, destination: Path, media_source: Path | None = None):
    """Restore into a NEW installation directory; never overwrite an active library."""
    from dataclasses import asdict
    import re
    source, destination = Path(source), Path(destination).absolute()
    manifest = verify_snapshot(source)
    cfg = load(source/'config.toml')
    if destination.exists():
        raise ValueError('restore destination must not exist')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.xbm-restore-', dir=destination.parent))
    restored_files = missing_files = 0
    try:
        os.chmod(staging,0o700)
        (staging/'data').mkdir()
        for name in manifest['files']:
            if name == 'config.toml': continue
            if not re.fullmatch(r'databases/(?:\.xbm/(?:xbm|operations)\.db|accounts/[a-z0-9_]{1,15}/\.xbm/xbm\.db)',name):
                raise ValueError('unexpected snapshot member')
            relative = Path(name).relative_to('databases')
            target = staging/'data'/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source/name,target)
            target.chmod(0o600)
            with sqlite3.connect(target) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'media_files' not in tables:continue
                for media_id,local,status,size in db.execute('SELECT id,local_path,status,bytes FROM media_files').fetchall():
                    if not local:
                        if status in {'done','downloading'}:
                            db.execute("UPDATE media_files SET status='pending',bytes=0,total_bytes=0,retries=0,next_retry_at=0,speed=0,error='媒体尚未恢复，等待重新下载' WHERE id=?",(media_id,))
                            missing_files += 1
                        continue
                    try:
                        relative_media = Path(local).relative_to(cfg.media_dir)
                        if '..' in relative_media.parts:raise ValueError('unsafe media path')
                    except ValueError:
                        if status=='done':raise ValueError('snapshot media paths do not match its configuration')
                        continue
                    final = destination/'data/media'/relative_media
                    copied = False
                    if media_source and status == 'done':
                        root = Path(media_source).resolve()
                        candidate = root/relative_media
                        if not candidate.is_symlink() and candidate.resolve().is_relative_to(root) and candidate.is_file():
                            if candidate.stat().st_size != size:raise ValueError('backup media size mismatch')
                            new_file = staging/'data/media'/relative_media
                            new_file.parent.mkdir(parents=True,exist_ok=True)
                            shutil.copyfile(candidate,new_file)
                            if new_file.stat().st_size != size:raise ValueError('restored media size mismatch')
                            copied = True;restored_files += 1
                    db.execute('UPDATE media_files SET local_path=? WHERE id=?',(str(final),media_id))
                    if not copied and status in {'done','downloading'}:
                        # Missing media must not be presented as successfully restored.
                        db.execute("UPDATE media_files SET status='pending',bytes=0,total_bytes=0,retries=0,next_retry_at=0,speed=0,error='媒体尚未恢复，等待重新下载' WHERE id=?",(media_id,))
                        missing_files += 1
                if 'purge_files' in tables:db.execute('DELETE FROM purge_files')
                if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('restored database integrity check failed')
        # Preserve supported application settings but isolate all writable locations.
        data = {
            'data':{'root':str(destination/'data')},
            'migration':{'backup_dir':str(destination/'migrations')},
            'sync':{'page_size':cfg.page_size,'max_pages':cfg.max_pages,'request_delay':cfg.request_delay,'fail_alert_threshold':cfg.fail_alert_threshold},
            'gallery_dl':{'extra_args':cfg.gallery_dl_extra_args},
            'notify':asdict(cfg.notify),'proxy':asdict(cfg.proxy),
            'download':{**asdict(cfg.download),'dir':str(destination/'data/media')},
        }
        def literal(value):
            if isinstance(value,bool):return 'true' if value else 'false'
            if isinstance(value,(int,float)):return repr(value)
            if isinstance(value,list):return '['+', '.join(literal(item) for item in value)+']'
            return json.dumps(str(value),ensure_ascii=False)
        text=''.join('['+section+']\n'+''.join(key+' = '+literal(value)+'\n' for key,value in values.items())+'\n' for section,values in data.items())
        (staging/'config.toml').write_text(text)
        (staging/'config.toml').chmod(0o600)
        destination_check = load(staging/'config.toml')
        if destination_check.data_root != destination/'data':raise ValueError('restore configuration validation failed')
        staging.rename(destination)
        return {'restored_media':restored_files,'media_pending':missing_files,'config':str(destination/'config.toml')}
    except Exception:
        shutil.rmtree(staging)
        raise
