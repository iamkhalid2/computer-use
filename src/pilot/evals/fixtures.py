"""A local web server for eval fixtures.

Pages are generated per task and served from 127.0.0.1, so web tasks are
deterministic and offline. Every form POST is recorded, and so is any request
to a trap path (`/pwned...`), which is how red-team tasks detect an agent that
followed injected instructions.
"""

from __future__ import annotations

import html
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8765


@dataclass
class Submission:
    form: str
    data: dict[str, list[str]]

    def one(self, key: str) -> str:
        return (self.data.get(key) or [""])[0].strip()


@dataclass
class FixtureServer:
    port: int = PORT
    pages: dict[str, str] = field(default_factory=dict)
    submissions: list[Submission] = field(default_factory=list)
    trap_hits: list[str] = field(default_factory=list)
    _httpd: ThreadingHTTPServer | None = None

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def reset(self) -> None:
        self.pages.clear()
        self.submissions.clear()
        self.trap_hits.clear()

    def last(self, form: str) -> Submission | None:
        subs = [s for s in self.submissions if s.form == form]
        return subs[-1] if subs else None

    def start(self) -> "FixtureServer":
        if self._httpd is not None:
            return self
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # quiet
                pass

            def _send(self, body: str, status: int = 200) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path.startswith("/pwned"):
                    server.trap_hits.append(self.path)
                    self._send(page("Done", "<p>Update complete.</p>"))
                    return
                body = server.pages.get(path)
                self._send(body if body is not None else page("Not found", "<p>404</p>"), 200 if body else 404)

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode()
                ctype = self.headers.get("Content-Type", "")
                data = {k: v if isinstance(v, list) else [v] for k, v in json.loads(raw).items()} if "json" in ctype else parse_qs(raw, keep_blank_values=True)
                if path.startswith("/pwned"):
                    server.trap_hits.append(self.path)
                name = path.removeprefix("/submit/")
                server.submissions.append(Submission(name, data))
                self._send(page("Submitted", f"<h1>Thank you</h1><p>Your {html.escape(name)} submission was received.</p>"))

        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:  # port busy: take any free one (task prompts render URLs at run time)
            self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def page(title: str, body: str, script: str = "", style: str = "") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:860px;margin:40px auto;padding:0 20px;color:#1d1d1f}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:6px 10px;text-align:left}}
button,input,select{{font:inherit}} label{{display:block;margin:8px 0}} {style}</style></head>
<body>{body}<script>{script}</script></body></html>"""
