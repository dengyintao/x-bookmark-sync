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
