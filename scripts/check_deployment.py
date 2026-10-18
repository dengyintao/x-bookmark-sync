#!/usr/bin/env python3
"""Check the running NAS version against an explicit release; never trust CLI exit 0."""
import argparse
import json
from urllib.request import urlopen
from urllib.parse import urlsplit


def verify(base, expected):
    parsed = urlsplit(base)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}:
        raise ValueError('provide an HTTP(S) origin without credentials')
    with urlopen(base.rstrip('/')+'/api/health', timeout=10) as response:
        status = json.load(response)
    if status.get('ok') is not True or status.get('service') != 'xbm':
        raise ValueError('unexpected service')
    if status.get('version') != expected:
        raise ValueError(f"upgrade did not take effect: expected {expected}, running {status.get('version')}")
    return {'ok': True, 'version': expected}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--expect', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.url, args.expect)))
    except Exception as error:
        parser.exit(1, 'Deployment verification failed: '+str(error)+'\n')
