"""Loopback dashboard and JSON API for the local contract directory."""
from __future__ import annotations

from contextlib import closing
import json
import mimetypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote

from . import core, email_audit

STATIC = Path(__file__).parent / 'static'


def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
        def local_origin(self):
            return f'http://127.0.0.1:{self.server.server_port}'

        def local_host(self):
            return self.headers.get('Host') == self.local_origin().removeprefix('http://')

        def send_json(self, payload, status=200):
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.local_host():
                return self.send_json({'error': 'Use the loopback address'}, 403)
            url = urlsplit(self.path)
            path = url.path
            if path.startswith('/api/'):
                try:
                    with closing(core.connect(db_path)) as con:
                        params = parse_qs(url.query)

                        def param(name, default=''):
                            return params.get(name, [default])[0]

                        if path == '/api/summary':
                            result = core.summary(con)
                        elif path == '/api/email-audit':
                            result = email_audit.audit_alerts(con, email_audit.load_alerts())
                        elif path == '/api/analytics':
                            result = core.analytics(con, param('state'))
                        elif path == '/api/config':
                            result = {'gates': core.GATES}
                        elif path == '/api/notices':
                            result = core.get_notices(
                                con, q=param('q'), source=param('source'), state=param('state'),
                                min_score=int(param('min_score', '0')), limit=int(param('limit', '50')),
                                offset=int(param('offset', '0')), view=param('view', 'all'),
                                category=param('category'))
                        elif path.startswith('/api/notice/'):
                            result = core.get_detail(con, unquote(path.removeprefix('/api/notice/')))
                            if result is None:
                                return self.send_json({'error': 'Notice not found'}, 404)
                        else:
                            return self.send_json({'error': 'Unknown endpoint'}, 404)
                    return self.send_json(result)
                except (ValueError, TypeError) as exc:
                    return self.send_json({'error': str(exc)}, 400)
            if path == '/favicon.ico':
                self.send_response(204)
                self.end_headers()
                return
            target = (STATIC / (path.removeprefix('/') or 'index.html')).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                return self.send_error(404)
            data = target.read_bytes()
            mime = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self):
            if not self.local_host() or self.headers.get('Origin') != self.local_origin():
                return self.send_json({'error': 'Local dashboard origin required'}, 403)
            prefix = '/api/notice/'
            path = urlsplit(self.path).path
            if not path.startswith(prefix) or not path.endswith('/review'):
                return self.send_json({'error': 'Unknown endpoint'}, 404)
            if self.headers.get_content_type() != 'application/json':
                return self.send_json({'error': 'Use application/json'}, 415)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    return self.send_json({'error': 'Invalid review request size'}, 400)
                body = json.loads(self.rfile.read(length))
                notice_id = unquote(path[len(prefix):-len('/review')])
                with closing(core.connect(db_path)) as con:
                    core.update_review(con, notice_id, body)
                return self.send_json({'saved': True})
            except (ValueError, TypeError) as exc:
                return self.send_json({'error': str(exc)}, 400)
            except LookupError as exc:
                return self.send_json({'error': str(exc)}, 404)
            except RuntimeError as exc:
                return self.send_json({'error': str(exc)}, 409)

    return Handler


def serve(db_path: Path, port: int = 8765):
    with closing(core.connect(db_path)), ThreadingHTTPServer(('127.0.0.1', port), make_handler(db_path)) as server:
        print(f'Contract Intelligence at http://127.0.0.1:{port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\nStopped')
