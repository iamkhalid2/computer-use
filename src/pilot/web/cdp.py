"""A minimal Chrome DevTools Protocol client over `--remote-debugging-pipe`.

The pipe is private to this process: no port is opened, so nothing else on the Mac can drive
the browser. Messages are JSON, each terminated by a NUL byte; Chrome reads commands on its fd 3
and writes replies and events on its fd 4.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import threading
import time
from typing import Any


class CDPError(RuntimeError):
    pass


class Pipe:
    def __init__(self, argv: list[str]):
        cmd_r, self._w = os.pipe()  # we write commands; Chrome reads them on fd 3
        self._r, resp_w = os.pipe()  # Chrome writes on fd 4; we read
        # Map the two ends onto fds 3 and 4 in the child with a shell redirection, after moving them
        # above 9 so neither is itself 3 or 4 (closing the original would then close the mapped one).
        cmd_r, resp_w = (self._high(fd) for fd in (cmd_r, resp_w))
        script = f"exec {shlex.join(argv)} 3<&{cmd_r} 4>&{resp_w} {cmd_r}<&- {resp_w}>&-"
        self.proc = subprocess.Popen(["/bin/sh", "-c", script], pass_fds=(cmd_r, resp_w), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        os.close(cmd_r)
        os.close(resp_w)
        self._next = 0
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._replies: dict[int, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.closed = False
        threading.Thread(target=self._read, name="cdp", daemon=True).start()

    @staticmethod
    def _high(fd: int) -> int:
        moved = fcntl.fcntl(fd, fcntl.F_DUPFD, 10)
        os.close(fd)
        return moved

    def _read(self) -> None:
        buf = b""
        while True:
            try:
                chunk = os.read(self._r, 1 << 16)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buf += chunk
            *messages, buf = buf.split(b"\0")
            for raw in messages:
                msg = json.loads(raw)
                with self._cv:
                    if "id" in msg:
                        self._replies[msg["id"]] = msg
                    else:
                        self.events.append(msg)
                    self._cv.notify_all()
        with self._cv:
            self.closed = True
            self._cv.notify_all()

    def call(self, method: str, params: dict[str, Any] | None = None, session: str | None = None, timeout: float = 15) -> dict[str, Any]:
        with self._lock:
            self._next += 1
            mid = self._next
            msg: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            os.write(self._w, json.dumps(msg).encode() + b"\0")
        deadline = time.monotonic() + timeout
        with self._cv:
            while mid not in self._replies:
                left = deadline - time.monotonic()
                if self.closed:
                    raise CDPError(f"browser exited during {method}")
                if left <= 0:
                    raise CDPError(f"{method} timed out after {timeout:g}s")
                self._cv.wait(left)
            reply = self._replies.pop(mid)
        if "error" in reply:
            raise CDPError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    def take(self, method: str | None = None) -> list[dict[str, Any]]:
        """Remove and return queued events (all, or just one method's)."""
        with self._cv:
            keep, out = [], []
            for e in self.events:
                (out if method is None or e.get("method") == method else keep).append(e)
            self.events = keep
        return out

    def close(self) -> None:
        try:
            self.call("Browser.close", timeout=3)
        except CDPError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        for fd in (self._w, self._r):
            try:
                os.close(fd)
            except OSError:
                pass
