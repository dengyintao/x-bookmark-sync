"""One-use dashboard intent tokens; issued without CORS so other sites cannot read them."""
import json
import secrets
import threading
import time


class DashboardTokens:
    def __init__(self):
        self._lock = threading.Lock()
        self._tokens = {}

    def issue(self):
        now = time.monotonic()
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens = {key: expiry for key, expiry in self._tokens.items() if expiry > now}
            if len(self._tokens) >= 1024:self._tokens.pop(next(iter(self._tokens)))
            self._tokens[token] = now+60
        return token

    def consume(self, token):
        if not isinstance(token,str) or len(token)>128:return False
        with self._lock:
            return self._tokens.pop(token,0)>time.monotonic()


def serve_token(handler):
    body=json.dumps({'ok':True,'token':handler.dashboard.issue()}).encode()
    handler.send_response(200)
    handler.send_header('Content-Type','application/json; charset=utf-8')
    handler.send_header('Content-Length',str(len(body)))
    handler.send_header('Cache-Control','no-store')
    handler.send_header('X-Content-Type-Options','nosniff')
    # Intentionally no Access-Control-Allow-Origin. A cross-origin GET cannot read the token.
    handler.end_headers()
    handler.wfile.write(body)
