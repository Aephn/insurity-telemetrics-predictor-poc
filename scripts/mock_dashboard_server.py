"""Minimal mock server to serve dashboard snapshot at /api/dashboard.
Run: PYTHONPATH=. python scripts/mock_dashboard_server.py
"""
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from urllib.parse import urlparse
from src.aws_lambda.dashboard_snapshot.handler import generate_snapshot

class Handler(BaseHTTPRequestHandler):
    def _set_cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):  # noqa: N802 - preflight
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == '/api/dashboard':
            snap = generate_snapshot()
            body = json.dumps(snap).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._set_cors()
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == '/healthz':
            body = b'OK'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self._set_cors()
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self._set_cors()
        self.end_headers()


def run_server(port: int = 8787):
    httpd = HTTPServer(('0.0.0.0', port), Handler)
    print(f"Mock dashboard server on http://localhost:{port}/api/dashboard (health: /healthz)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        print('\nServer stopping...')
    finally:
        httpd.server_close()
        print('Server shutdown.')

if __name__ == '__main__':
    run_server()
