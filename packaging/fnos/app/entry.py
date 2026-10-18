"""Frozen application and gallery-dl entry points."""
import sys
if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--backup-metadata':
        from pathlib import Path
        from xbm.backup import create_snapshot, verify_snapshot
        create_snapshot(Path(sys.argv[2]), Path(sys.argv[3]))
        verify_snapshot(Path(sys.argv[3]))
        print('Metadata backup verified; media files are not included.')
        sys.exit(0)
    if len(sys.argv) == 4 and sys.argv[1] == '--check-service':
        import json
        import time
        import urllib.request
        from xbm import __version__
        for attempt in range(15):
            try:
                with urllib.request.urlopen(sys.argv[2].rstrip('/') + '/api/status', timeout=2) as response:
                    status = json.load(response)
                if status.get('service') != 'xbm' or status.get('version') != __version__:
                    raise ValueError('Running service version differs from installed program')
                from pathlib import Path
                from xbm.config import load
                expected = load(Path(sys.argv[3])).media_dir.resolve()
                if Path(status['media_dir']).resolve() != expected:
                    raise ValueError('Running service uses unexpected media storage')
                sys.exit(0)
            except Exception as error:
                if attempt == 14:
                    print('Service verification failed: ' + str(error), file=sys.stderr)
                    sys.exit(1)
                time.sleep(1)
    if len(sys.argv) == 4 and sys.argv[1] == '--write-fnos-config':
        import json
        from pathlib import Path
        config, root = Path(sys.argv[2]), Path(sys.argv[3])
        config.write_text('[data]\nroot = ' + json.dumps(str(root / 'data')) +
                          '\n[migration]\nbackup_dir = ' + json.dumps(str(root / 'migrations')) + '\n')
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == '--gallery-dl':
        sys.argv.pop(1)
        import gallery_dl
        gallery_dl.main()
    else:
        from xbm.cli import main
        main()
