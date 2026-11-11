"""The window's back end: a tiny HTTP server on this Mac only.

It can start tasks that control the computer, so it trusts nobody by default:
  * it listens on 127.0.0.1 and answers only requests addressed to that exact host and port
    (a web page that rebinds a domain name to this machine is refused);
  * every API call must carry a secret made at launch, which only Pilot's own page is given;
  * it sends no cross-origin headers, so no other page can read its replies.
"""

from __future__ import annotations

import hmac
import json
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .session import Session, readiness

PAGE = Path(__file__).with_name("page.html")
MAX_BODY = 64 * 1024
PANES = {"control": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
         "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"}
EXAMPLES = ["Organise my Downloads folder", "Find jobs for me and apply", "Book a flight for me"]


class UIServer:
    def __init__(self, session: Session, port: int = 0, on_restart: Callable[[], None] | None = None,
                 on_quit: Callable[[], None] | None = None):
        self.session = session
        self.token = secrets.token_urlsafe(24)
        self.on_restart, self.on_quit = on_restart, on_quit
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"

    def start(self) -> "UIServer":
        threading.Thread(target=self.httpd.serve_forever, name="pilot-ui-server", daemon=True).start()
        return self

    # -- actions the page can ask for ---------------------------------------------------
    def setup(self, what: str) -> dict[str, Any]:
        """Start one first-run step. Pilot never changes a setting itself: it opens the right place and the user decides."""
        if what == "control":
            from ..mac.ax import AX

            AX().trusted(prompt=True)  # macOS shows its own "allow Pilot to control this computer" prompt
            subprocess.run(["open", PANES["control"]], check=False)
        elif what == "screen":
            from ..mac.screen import Screen

            Screen().request_permission()
            subprocess.run(["open", PANES["screen"]], check=False)
        elif what == "model":
            from ..providers.claude_code import find_cli

            cli = find_cli()
            if not cli:
                subprocess.run(["open", "https://claude.com/claude-code"], check=False)
                return {"ok": True, "note": "Install Claude Code first, then come back and press Check again."}
            script = Path.home() / "Library" / "Caches" / "pilot-sign-in.command"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(f"#!/bin/zsh\nclear\necho 'Signing Pilot in to Claude…'\n'{cli}' auth login\necho\necho 'You can close this window.'\n")
            script.chmod(0o700)
            subprocess.run(["open", str(script)], check=False)
        elif what == "restart" and self.on_restart:
            threading.Timer(0.3, self.on_restart).start()
        else:
            return {"ok": False}
        return {"ok": True}

    def handle(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        s = self.session
        if method == "GET" and path == "/api/state":
            return 200, {**readiness(), "state": s.state, "examples": EXAMPLES}
        if method == "GET" and path == "/api/events":
            try:
                after = int(body.get("after", 0))
            except (TypeError, ValueError):
                after = 0
            return 200, {"events": s.events_after(after, timeout=min(float(body.get("wait", 25)), 25))}
        if method != "POST":
            return 404, {"error": "not found"}
        if path == "/api/task":
            return (200, {"ok": True}) if s.submit(str(body.get("task", ""))[:4000]) else (409, {"error": "busy, or nothing to do"})
        if path == "/api/answer":
            return (200, {"ok": True}) if s.answer(str(body.get("id", "")), body.get("value")) else (409, {"error": "no such question"})
        if path == "/api/stop":
            s.request_stop()
            return 200, {"ok": True}
        if path == "/api/rate":
            return 200, {"ok": s.rate(str(body.get("rating", "")), str(body.get("note", ""))[:1000])}
        if path == "/api/setup":
            return 200, self.setup(str(body.get("what", "")))
        if path == "/api/quit" and self.on_quit:
            threading.Timer(0.2, self.on_quit).start()
            return 200, {"ok": True}
        return 404, {"error": "not found"}

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        ui = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # the task log is the log; requests are noise
                pass

            def _send(self, code: int, payload: Any, kind: str = "application/json") -> None:
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", kind + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(data)

            def _guard(self, api: bool) -> bool:
                if self.headers.get("Host") != f"127.0.0.1:{ui.port}":
                    self._send(403, {"error": "wrong host"})
                    return False
                if api and not hmac.compare_digest(self.headers.get("X-Pilot-Token", ""), ui.token):
                    self._send(403, {"error": "not Pilot's own page"})
                    return False
                return True

            def do_GET(self) -> None:  # noqa: N802
                path, _, query = self.path.partition("?")
                if path == "/":
                    if self._guard(api=False):
                        self._send(200, PAGE.read_text().replace("__PILOT_TOKEN__", ui.token).encode(), "text/html")
                    return
                if not self._guard(api=True):
                    return
                params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                try:
                    self._send(*ui.handle("GET", path, params))
                except Exception as e:  # noqa: BLE001
                    self._send(500, {"error": f"{type(e).__name__}: {e}"[:300]})

            def do_POST(self) -> None:  # noqa: N802
                if not self._guard(api=True):
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        return self._send(413, {"error": "too large"})
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("body must be an object")
                except (ValueError, json.JSONDecodeError):
                    return self._send(400, {"error": "bad request"})
                try:
                    self._send(*ui.handle("POST", self.path.partition("?")[0], body))
                except Exception as e:  # noqa: BLE001
                    self._send(500, {"error": f"{type(e).__name__}: {e}"[:300]})

        return Handler
