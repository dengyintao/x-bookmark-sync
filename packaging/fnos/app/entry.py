"""Frozen application and gallery-dl entry points."""
import sys
if __name__ == '__main__':
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
