"""Local single-user HTTP workspace with same-origin mutation protection."""

from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import secrets
import socket
from urllib.parse import parse_qs, urlsplit

from .decision import demo_payload
from .store import BudgetError, ConflictError


STATIC = Path(__file__).parent / "static"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def __init__(self, *args, app, csrf, **kwargs):
        self.app, self.csrf = app, csrf
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        # Never log user content or request bodies.
        pass

    def trusted(self, mutation=False):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in hosts or (origin and origin not in {"http://" + h for h in hosts}):
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        return not mutation or hmac.compare_digest(self.headers.get("X-Dao-CSRF", ""), self.csrf)

    def headers_for(self, status, mime, length=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.headers_for(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def do_GET(self):
        if not self.trusted():
            return self.respond(403, {"error": "Untrusted host or origin"})
        parsed = urlsplit(self.path)
        branch = parse_qs(parsed.query).get("branch", ["main"])[0]
        try:
            if parsed.path in {"/api/bootstrap", "/api/state"}:
                self.respond(200, {**self.app.snapshot(branch), "csrf": self.csrf})
            elif parsed.path == "/api/decision-example":
                self.respond(200, demo_payload())
            elif parsed.path == "/api/verify":
                self.respond(200, self.app.store.verify())
            elif parsed.path == "/api/export":
                self.respond(200, {"schema": "dao-export-v1", **self.app.snapshot(branch)})
            elif parsed.path in {"/", "/index.html", "/app.js", "/style.css", "/static/app.js", "/static/style.css", "/static/dao.svg", "/static/dao.ico"}:
                filename = "index.html" if parsed.path == "/" else parsed.path.rsplit("/", 1)[-1]
                body = (STATIC / filename).read_bytes()
                extension = filename.rsplit(".", 1)[-1]
                mime = {"html": "text/html", "js": "text/javascript", "css": "text/css", "svg": "image/svg+xml", "ico": "image/vnd.microsoft.icon"}[extension]
                if extension != "ico":
                    mime += "; charset=utf-8"
                self.headers_for(200, mime, len(body))
                self.wfile.write(body)
            else:
                self.respond(404, {"error": "Not found"})
        except (ValueError, KeyError) as exc:
            self.respond(400, {"error": str(exc)})

    def do_POST(self):
        if not self.trusted(mutation=True):
            self.close_connection = True
            return self.respond(403, {"error": "Untrusted host, origin, or CSRF token"})
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Transfer encoding is unsupported")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 131072:
                raise ValueError("Request body must be 1..131072 bytes")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Content-Type must be application/json")
            data = json.loads(self.rfile.read(length), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))
            if not isinstance(data, dict):
                raise ValueError("Request must be a JSON object")
            path = urlsplit(self.path).path
            if path == "/api/chat":
                stream = self.app.chat(data)
                first = next(stream)  # Admission errors retain ordinary HTTP status codes.
                self.send_response(200)
                self.send_header("Connection", "close")
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.close_connection = True
                disconnected = False
                for event in _prepend(first, stream):
                    if disconnected:
                        continue  # Complete accounting and persist the result after browser disconnect.
                    try:
                        self.wfile.write((json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        disconnected = True
            else:
                self.respond(200, self.app.mutate(path, data))
        except (ConflictError, BudgetError) as exc:
            self.respond(409, {"error": str(exc)})
        except (ValueError, TypeError, KeyError, StopIteration) as exc:
            self.close_connection = True
            self.respond(400, {"error": str(exc)})


def _prepend(first, iterator):
    yield first
    yield from iterator


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(app, port=8765):
    server = LocalServer(("127.0.0.1", port), partial(Handler, app=app, csrf=secrets.token_urlsafe(32)))
    server.daemon_threads = False
    return server
