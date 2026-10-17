#!/usr/bin/env python3
"""Build and verify the release assets locally or in GitHub Actions (Python 3.11+)."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def parse_manifest(text):
    return {key.strip(): value.strip() for line in text.splitlines() if '=' in line
            for key, value in [line.split('=', 1)]}


def version_info(tag=''):
    version = tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
    manifest = parse_manifest((ROOT/'packaging/fnos/manifest').read_text())
    if f'__version__ = "{version}"' not in (ROOT/'src/xbm/__init__.py').read_text():
        raise ValueError('Runtime version must match Python package version')
    if manifest['version'] != version:
        raise ValueError('FPK manifest version must match Python package version')
    if tag and tag != 'v' + version:
        raise ValueError(f'Release tag {tag} must equal v{version}')
    extension = json.loads((ROOT/'extension/manifest.json').read_text())['version']
    return version, extension, manifest


def verify_fpk(path, manifest):
    with tarfile.open(path, 'r:gz') as package:
        file = package.extractfile('manifest')
        if file is None:
            raise ValueError('FPK manifest is missing')
        actual = parse_manifest(file.read().decode())
        for field in ('appname', 'version', 'platform'):
            if actual[field] != manifest[field]:
                raise ValueError('FPK metadata mismatch: ' + field)
        if not package.getmember('cmd/main').mode & 0o111:
            raise ValueError('FPK lifecycle script is not executable')
        with tarfile.open(fileobj=package.extractfile('app.tgz'), mode='r:gz') as payload:
            for required in ('server/xbm', 'ffmpeg', 'gallery-dl'):
                if not payload.getmember(required).mode & 0o111:
                    raise ValueError('FPK standalone executable missing: ' + required)
            ui = json.loads(payload.extractfile('ui/config').read())
            if manifest['desktop_applaunchname'] not in ui['.url']:
                raise ValueError('FPK desktop entry is missing')
            for icon in ('ui/images/icon_64.png','ui/images/icon_256.png'):
                if payload.getmember(icon).size <= 0:
                    raise ValueError('FPK desktop icon is empty')
        for name in ('ICON.PNG', 'ICON_256.PNG'):
            if package.getmember(name).size <= 0:
                raise ValueError('FPK icon is empty')


def build(fnpack, output, tag=''):
    version, extension_version, manifest = version_info(tag)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='xbm-release-') as tmp:
        staging = Path(tmp)
        application = staging/'fpk-source'
        shutil.copytree(ROOT/'packaging/fnos', application)
        if platform.system() != 'Linux' or platform.machine() != 'x86_64':
            raise RuntimeError('Standalone FPK must be built on Linux x86_64 (use GitHub Actions)')
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
            '--onedir', '--name', 'xbm', '--distpath', str(staging/'frozen'),
            '--workpath', str(staging/'pyinstaller'), '--specpath', str(staging),
            '--collect-all', 'xbm', '--collect-all', 'twikit', '--collect-all', 'gallery_dl',
            str(ROOT/'packaging/fnos/app/entry.py')], cwd=ROOT, check=True)
        shutil.copytree(staging/'frozen/xbm', application/'app/server')
        import imageio_ffmpeg
        shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), application/'app/ffmpeg')
        (application/'app/ffmpeg').chmod(0o755)
        subprocess.run([str(application/'app/server/xbm'), '--help'], check=True)
        subprocess.run([str(application/'app/server/xbm'), '--gallery-dl', '--version'], check=True)
        subprocess.run([str(application/'app/ffmpeg'), '-version'], check=True)
        subprocess.run([sys.executable, str(ROOT/'scripts/test_fpk.py'), str(application)], check=True)
        subprocess.run([str(fnpack.resolve()), 'build', '-d', str(application)], cwd=staging, check=True)
        fpk = staging/(manifest['appname']+'.fpk')
        verify_fpk(fpk, manifest)
        shutil.copy2(fpk, output/f'x-bookmark-sync-{version}.fpk')
        # Build a wheel without bundling runtime dependencies or private config.
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '.', '-w', str(staging/'wheels')], cwd=ROOT, check=True)
        wheel = next((staging/'wheels').glob('xbm-*.whl'))
        with zipfile.ZipFile(wheel) as archive:
            required = ['xbm/server.py','xbm/migration.py','xbm/web/index.html','xbm/web/app.js','xbm/web/style.css']
            if not all(name in archive.namelist() for name in required):
                raise ValueError('Wheel is missing NAS dashboard or migration assets')
        shutil.copy2(wheel, output/wheel.name)
        extension_zip = output/f'x-bookmark-extension-{extension_version}.zip'
        with zipfile.ZipFile(extension_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
            for file in sorted((ROOT/'extension').iterdir()):
                if file.is_file() and file.suffix in {'.js','.json','.html','.md'}:
                    archive.write(file, file.name)
        with zipfile.ZipFile(extension_zip) as archive:
            if json.loads(archive.read('manifest.json'))['version'] != extension_version:
                raise ValueError('Extension archive version mismatch')
        # Archive committed sources only. Credentials, config.toml and local data
        # never enter the source package; scripts and FPK sources are included.
        source_tar = staging/'source.tar'
        subprocess.run(['git','archive','--format=tar',f'--prefix=xbm-{version}/','HEAD','-o',str(source_tar)],cwd=ROOT,check=True)
        with source_tar.open('rb') as source, gzip.open(output/f'xbm-{version}-source.tar.gz','wb') as destination:
            shutil.copyfileobj(source,destination)
    assets = sorted(path for path in output.iterdir() if path.is_file() and path.name != 'SHA256SUMS')
    (output/'SHA256SUMS').write_text(''.join(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name+'\n' for path in assets))
    return assets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fnpack', default=os.environ.get('FNPACK_BIN') or shutil.which('fnpack'))
    parser.add_argument('--out', type=Path, default=ROOT/'dist/release')
    parser.add_argument('--tag', default=os.environ.get('RELEASE_TAG',''))
    args = parser.parse_args()
    if not args.fnpack:
        parser.error('Install official fnpack 1.2.3 or provide --fnpack /path/to/fnpack')
    for asset in build(Path(args.fnpack), args.out, args.tag):
        print(asset.name)


if __name__ == '__main__':
    main()
