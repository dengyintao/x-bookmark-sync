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
expected_version = dict(line.split('=', 1) for line in (package/'manifest').read_text().splitlines() if '=' in line)
expected_version = {k.strip(): v.strip() for k, v in expected_version.items()}['version']
with tempfile.TemporaryDirectory(prefix='xbm-fpk-smoke-') as temporary:
    root = Path(temporary)
    env = dict(os.environ, TRIM_APPDEST=str(package/'app'),
               TRIM_PKGVAR=str(root/'var'), TRIM_PKGETC=str(root/'etc'),
               TRIM_TEMP_LOGFILE=str(root/'errors.log'), wizard_admin_password='Synthetic-test-password-2026')
    def hook(name, *args, check=True):
        return subprocess.run([str(package/'cmd'/name), *args], env=env, check=check)
    def headers():
        return {'Content-Type':'application/json', 'Authorization':'Bearer '+(root/'var/data/.xbm/admin-key.txt').read_text().strip()}
    def get(path):
        with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8765' + path, headers=headers()), timeout=10) as response:
            return response.read()
    try:
        hook('install_init')
        hook('install_callback')
        original = (root/'etc/config.toml').read_bytes()
        hook('main', 'start')
        hook('main', 'status')
        assert b'<html' in get('/').lower()
        import stat
        assert stat.S_ISSOCK((package/'app/app.sock').stat().st_mode)
        assert (root/'var/data/.xbm/admin-key.txt').stat().st_mode & 0o777 == 0o600
        assert json.loads(get('/api/status'))['version'] == expected_version
        hook('main', 'start')  # idempotent
        # Persist a synthetic bookmark and a media sentinel before lifecycle upgrade.
        request = urllib.request.Request('http://127.0.0.1:8765/api/ingest',
            data=json.dumps({'account':'x:fixture','tweets':[{'tweet_id':'42','text':'upgrade fixture'}]}).encode(),
            headers=headers())
        with urllib.request.urlopen(request) as response:
            assert json.load(response)['ok']
        sentinel = root/'var/data/media-sentinel'
        sentinel.write_bytes(b'preserve media')
        hook('upgrade_init')
        assert hook('main', 'status', check=False).returncode == 3
        hook('upgrade_callback')
        assert (root/'etc/config.toml').read_bytes() == original
        hook('main', 'start')
        assert json.loads(get('/api/status?account=x:fixture'))['tweets'] == 1
        assert sentinel.read_bytes() == b'preserve media'
        subprocess.run([str(package/'app/server/xbm'), '--backup-metadata',
                        str(root/'etc/config.toml'), str(root/'backup')], check=True)
        assert (root/'backup/databases/accounts/fixture/.xbm/xbm.db').exists()
        assert not (root/'backup/media-sentinel').exists()
        hook('uninstall_init')
        assert (root/'etc/config.toml').exists()
        assert (root/'var/data').exists()
        assert hook('main', 'status', check=False).returncode == 3
        print('Standalone FPK lifecycle and HTTP smoke tests passed')
    finally:
        hook('main', 'stop', check=False)
