#!/usr/bin/env python3
"""Exercise packaged hooks and HTTP UI with empty isolated data; never contact X."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

package = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='xbm-fpk-smoke-') as temporary:
    root = Path(temporary)
    env = dict(os.environ, TRIM_APPDEST=str(package/'app'),
               TRIM_PKGVAR=str(root/'var'), TRIM_PKGETC=str(root/'etc'),
               TRIM_TEMP_LOGFILE=str(root/'errors.log'))
    def hook(name, *args, check=True):
        return subprocess.run([str(package/'cmd'/name), *args], env=env, check=check)
    def get(path):
        with urllib.request.urlopen('http://127.0.0.1:8765' + path, timeout=10) as response:
            return response.read()
    try:
        hook('install_init')
        hook('install_callback')
        original = (root/'etc/config.toml').read_bytes()
        hook('main', 'start')
        hook('main', 'status')
        assert b'<html' in get('/').lower()
        assert isinstance(json.loads(get('/api/status')), dict)
        hook('main', 'start')  # idempotent
        hook('upgrade_init')
        assert hook('main', 'status', check=False).returncode == 3
        hook('upgrade_callback')
        assert (root/'etc/config.toml').read_bytes() == original
        hook('main', 'start')
        hook('uninstall_init')
        assert (root/'etc/config.toml').exists()
        assert (root/'var/data').exists()
        assert hook('main', 'status', check=False).returncode == 3
        print('Standalone FPK lifecycle and HTTP smoke tests passed')
    finally:
        hook('main', 'stop', check=False)
