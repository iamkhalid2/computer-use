"""Apps, windows, AppleScript and shell: the agent's high-level shortcuts.

One `open -a Safari https://...` or a short AppleScript can replace twenty GUI
steps. Fewer steps is the single biggest speed lever for a computer-use agent.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
DOMAIN_RE = re.compile(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?$", re.I)


@dataclass
class RunResult:
    ok: bool
    output: str
    code: int


def _clip(text: str, limit: int = 4000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit // 2] + f"\n…[{len(text) - limit} chars omitted]…\n" + text[-limit // 2 :]


def run(cmd: list[str] | str, timeout: float = 30.0, shell: bool = False, stdin: str | None = None) -> RunResult:
    try:
        p = subprocess.run(
            cmd, shell=shell, capture_output=True, text=True, timeout=timeout, input=stdin,
            executable="/bin/zsh" if shell else None,
        )
    except subprocess.TimeoutExpired:
        return RunResult(False, f"timed out after {timeout:.0f}s", -1)
    except FileNotFoundError as e:
        return RunResult(False, str(e), -1)
    out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr.strip() else "")
    return RunResult(p.returncode == 0, _clip(out) or ("(no output)" if p.returncode == 0 else f"exit {p.returncode}"), p.returncode)


def open_target(target: str, app: str | None = None) -> RunResult:
    """Open an app by name, a URL, or a file/folder path."""
    t = target.strip()
    expanded = os.path.expanduser(t)
    if URL_RE.match(t) or DOMAIN_RE.match(t):
        url = t if URL_RE.match(t) else f"https://{t}"
        cmd = ["open", "-a", app, url] if app else ["open", url]
    elif Path(expanded).exists():
        cmd = ["open", "-a", app, expanded] if app else ["open", expanded]
    else:
        cmd = ["open", "-a", t]
    res = run(cmd, timeout=20)
    if res.ok:
        return RunResult(True, f"opened {t}" + (f" with {app}" if app else ""), 0)
    if cmd[:2] == ["open", "-a"] and not app:
        # Not an installed app name: fall back to Spotlight-style lookup by bundle name.
        found = run(["mdfind", f"kMDItemContentType == 'com.apple.application-bundle' && kMDItemDisplayName == '{t}*'c"], timeout=5)
        paths = [line for line in found.output.splitlines() if line.endswith(".app")]
        if paths:
            r2 = run(["open", paths[0]], timeout=20)
            if r2.ok:
                return RunResult(True, f"opened {Path(paths[0]).stem}", 0)
    return res


def applescript(script: str, timeout: float = 30.0) -> RunResult:
    return run(["osascript", "-"], timeout=timeout, stdin=script)


def shell(command: str, timeout: float = 30.0) -> RunResult:
    return run(command, timeout=timeout, shell=True)


def visible_windows(limit: int = 10) -> list[str]:
    """On-screen windows, front to back: "App — Title". Needs no AX permission."""
    try:
        import Quartz
    except Exception:
        return []
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    info = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    out: list[str] = []
    seen: set[str] = set()
    for w in info:
        if w.get("kCGWindowLayer", 1) != 0:
            continue
        owner = str(w.get("kCGWindowOwnerName", "") or "")
        title = str(w.get("kCGWindowName", "") or "")
        b = w.get("kCGWindowBounds") or {}
        if float(b.get("Width", 0)) < 60 or float(b.get("Height", 0)) < 40:
            continue
        label = f"{owner} — {title}" if title else owner
        if label in seen or not owner:
            continue
        seen.add(label)
        out.append(label)
        if len(out) >= limit:
            break
    return out


def screen_locked() -> bool:
    """True while the login window covers the session (idle lock, lid, fast user switch)."""
    try:
        import Quartz

        info = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(info.get("CGSSessionScreenIsLocked")) or info.get("kCGSSessionOnConsoleKey") is False
    except Exception:
        return False


class KeepAwake:
    """Hold display, idle and user-activity assertions for as long as this process lives.

    `caffeinate -w <pid>` releases automatically when Pilot exits, even on a crash,
    so a long run can't be killed by the idle lock and nothing is left behind.
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "KeepAwake":
        try:
            self.proc = subprocess.Popen(["caffeinate", "-dimsu", "-w", str(os.getpid())],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            self.proc = None
        return self

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def notify(title: str, message: str) -> None:
    safe = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    applescript(f'display notification "{safe(message)}" with title "{safe(title)}"', timeout=5)


def say(text: str) -> None:
    try:
        subprocess.Popen(["say", text[:400]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
