"""`pilot app`: the window a person uses instead of the command line."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from .server import UIServer
from .session import Session

STATE = Path.home() / "Library" / "Caches" / "pilot-app.json"
RESTART_CODE = 75  # the launcher in Pilot.app starts Pilot again when it exits with this


def running() -> dict | None:
    """The Pilot window already open on this Mac, if there is one."""
    try:
        info = json.loads(STATE.read_text())
        os.kill(int(info["pid"]), 0)
        with urllib.request.urlopen(f"http://127.0.0.1:{int(info['port'])}/", timeout=1.5) as r:
            return info if r.status == 200 else None
    except Exception:  # noqa: BLE001 - stale file, dead process, port reused by something else
        return None


def restart() -> None:
    if os.environ.get("PILOT_LAUNCHER") == "1":
        os._exit(RESTART_CODE)  # Pilot.app's launcher starts a fresh copy, so macOS re-reads its permissions
    os.execv(sys.executable, [sys.executable, "-m", "pilot", "app", *sys.argv[2:]])


def main(browser: bool = False, port: int = 0) -> int:
    if running() is not None:
        print("Pilot is already open.")
        return 0
    session = Session()
    quit_hook = (lambda: os._exit(0)) if browser else None
    server = UIServer(session, port, on_restart=restart, on_quit=quit_hook).start()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"pid": os.getpid(), "port": server.port, "since": time.strftime("%Y-%m-%dT%H:%M:%S")}))
    print(f"Pilot window: {server.url}", flush=True)
    try:
        if browser:
            import webbrowser

            webbrowser.open(server.url)
            while True:
                time.sleep(3600)
        from .window import Window

        Window(server.url, session).run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            STATE.unlink()
        except OSError:
            pass
    return 0
