"""Harness-level safety. It does not rely on the model behaving.

* Kill switch: slam the mouse into the top-left corner to stop the run.
* Hard blocks: destructive shell patterns, typing into the terminal running Pilot.
* Confirmation: risky clicks (Send, Delete, Buy...), password fields, non-read-only
  shell, AppleScript that sends, deletes or runs shell commands.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

RISKY_LABEL = re.compile(
    r"\b(send|delete|remove|erase|purchase|buy|pay|place order|checkout|check out|submit|transfer|"
    r"empty trash|move to trash|sign out|log out|uninstall|format|publish|post|tweet|reply all|"
    r"confirm payment|subscribe|unsubscribe|wipe|reset|discard|don.t save|replace)\b",
    re.I,
)

BLOCK_SHELL = [
    r"\bsudo\b", r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)(\s|$)", r"\bmkfs", r"\bdiskutil\s+(erase|partition|zero)",
    r":\(\)\s*\{", r"\bdd\s+if=", r">\s*/dev/(disk|rdisk)", r"\b(shutdown|reboot|halt)\b", r"\bcsrutil\b",
    r"\bspctl\s+--master-disable", r"\bnvram\b", r"\btmutil\s+delete", r"\bsecurity\s+(delete|dump)",
    r"curl[^|]*\|\s*(sh|bash|zsh)", r"\blaunchctl\s+(unload|remove|bootout)",
]
READ_ONLY_CMDS = {
    "ls", "cat", "head", "tail", "wc", "pwd", "echo", "date", "cal", "whoami", "which", "where", "type",
    "file", "stat", "du", "df", "mdfind", "mdls", "grep", "rg", "find", "sw_vers", "uname", "pbpaste",
    "defaults", "plutil", "sort", "uniq", "cut", "tr", "awk", "sed", "jq", "ps", "pgrep", "lsof", "uptime",
    "system_profiler", "networksetup", "scutil", "ifconfig", "ping", "dig", "host", "textutil", "open",
    "basename", "dirname", "realpath", "test", "true", "env", "printenv", "osascript",
}
WRITE_FLAGS = {"find": {"-delete", "-exec", "-execdir", "-ok"}, "sed": {"-i"}, "defaults": {"write", "delete", "import"},
               "networksetup": {"-set", "-setairportpower", "-setdnsservers"}, "osascript": {"*"}}
RISKY_SCRIPT = re.compile(
    r"\b(delete|empty trash|send|purchase|shut ?down|restart|log ?out|erase|do shell script|move .* to trash|"
    r"set volume|keystroke .*return.*send)\b",
    re.I,
)


@dataclass
class Verdict:
    level: str  # ok | confirm | block
    reason: str = ""


# Verbs whose mention in the user's own request authorises the matching button.
SCOPE_VERBS = ["submit", "send", "delete", "remove", "move to trash", "post", "publish", "subscribe", "unsubscribe",
               "replace", "discard", "reset", "transfer", "pay", "buy", "purchase", "checkout", "sign out", "log out"]
FILE_WRITERS = {"mkdir", "mv", "cp", "touch", "tee", "ln"}
FILE_DELETERS = {"rm", "rmdir"}
CURL_WRITE_FLAGS = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "-F", "--form", "-T", "--upload-file",
                    "-X", "--request", "-o", "--output", "-O", "--remote-name", "-K", "--config"}


@dataclass
class TaskScope:
    """What the user's own request authorises. Built only from the user's words (and the
    operator's configured roots), never from screen content, so injected text can't widen it."""

    origins: set[str] = field(default_factory=set)  # scheme://host:port of URLs in the task
    roots: list[Path] = field(default_factory=list)  # folders the task names (or configured write roots)
    verbs: set[str] = field(default_factory=set)

    @classmethod
    def from_task(cls, task: str, extra_roots: list[str] | None = None) -> "TaskScope":
        origins = set()
        for url in re.findall(r"https?://[^\s'\"<>)]+", task):
            u = urlparse(url.rstrip(".,;:"))
            origins.add(f"{u.scheme}://{u.netloc}")
        roots: list[Path] = []
        for raw in re.findall(r"(~/[^\s,;'\"]+|/Users/[^\s,;'\"]+)", task):
            path = Path(os.path.expanduser(raw.rstrip(".,;:)"))).resolve()
            roots.append(path.parent if path.suffix and not path.is_dir() else path)
        roots += [Path(os.path.expanduser(r)).resolve() for r in (extra_roots or [])]
        low = task.lower()
        verbs = {v for v in SCOPE_VERBS if re.search(rf"\b{re.escape(v)}\b", low)}
        return cls(origins, roots, verbs)

    def allows_url(self, url: str) -> bool:
        u = urlparse(url)
        return f"{u.scheme}://{u.netloc}" in self.origins

    def allows_path(self, raw: str) -> bool:
        path = Path(os.path.expanduser(raw)).resolve()
        return any(path == r or r in path.parents for r in self.roots)

    def allows_label(self, label: str) -> bool:
        low = (label or "").lower()
        return any(re.search(rf"\b{re.escape(v)}\b", low) for v in self.verbs)


def classify_shell(command: str, scope: TaskScope | None = None) -> Verdict:
    for pat in BLOCK_SHELL:
        if re.search(pat, command, re.I):
            return Verdict("block", f"blocked destructive command pattern ({pat})")
    stripped = re.sub(r"[12]?>\s*/dev/null|2>&1", "", command)
    # Redirection into a folder the task names is just a scoped write.
    if scope is not None:
        stripped = re.sub(r">>?\s*(['\"]?)([^\s'\"]+)\1", lambda m: "" if scope.allows_path(m.group(2)) else m.group(0), stripped)
    if re.search(r"`|\$\(|\$\{|>|<\(", stripped):
        return Verdict("confirm", "shell substitution or redirection")
    cwd: str | None = None
    for segment in re.split(r"\|\||&&|;|\||&", stripped):
        v, cwd = _segment_verdict(segment, scope, cwd)
        if v.level != "ok":
            return v
    return Verdict("ok")


def _in_scope(arg: str, scope: TaskScope, cwd: str | None) -> bool:
    if not arg.startswith(("/", "~")) and cwd:
        arg = os.path.join(cwd, arg)
    return arg.startswith(("/", "~")) and scope.allows_path(arg)


def _segment_verdict(segment: str, scope: TaskScope | None = None, cwd: str | None = None) -> tuple[Verdict, str | None]:
    try:
        words = shlex.split(segment)
    except ValueError:
        return Verdict("confirm", "unparseable shell command"), cwd
    if not words:
        return Verdict("ok"), cwd
    cmd = words[0].rsplit("/", 1)[-1]
    args = [w for w in words[1:] if not w.startswith("-")]
    if cmd == "cd":
        return Verdict("ok"), (args[0] if args else cwd)
    if scope is not None and cmd in {"curl", "wget"}:
        urls = [a for a in args if a.startswith(("http://", "https://"))]
        if urls and all(scope.allows_url(u) for u in urls) and not CURL_WRITE_FLAGS & set(words[1:]):
            return Verdict("ok"), cwd  # reading a page the user pointed at
    if scope is not None and (cmd in FILE_WRITERS or (cmd in FILE_DELETERS and scope.verbs & {"delete", "remove"})):
        paths = [a for a in args if a not in {"-p"}]
        if paths and all(_in_scope(a, scope, cwd) for a in paths):
            return Verdict("ok"), cwd  # writing inside a folder the user named
    if cmd not in READ_ONLY_CMDS:
        return Verdict("confirm", f"`{cmd}` can change things"), cwd
    bad = WRITE_FLAGS.get(cmd, set())
    if "*" in bad or any(w in bad for w in words[1:]):
        return Verdict("confirm", f"`{cmd}` with write options"), cwd
    return Verdict("ok"), cwd


def classify_script(script: str) -> Verdict:
    m = RISKY_SCRIPT.search(script)
    return Verdict("confirm", f"AppleScript uses '{m.group(0)}'") if m else Verdict("ok")


# Only controls that *trigger* something can send, buy or delete. Toggles, fields and
# text never can ("Send me the newsletter" is a checkbox, not a send button).
TRIGGER_ROLES = {"AXButton", "AXMenuItem", "AXMenuBarItem", "AXLink", "AXMenuButton", "AXPopUpButton", "AXCell", "AXUnknown", None}


def classify_label(label: str, role: str | None = None, scope: TaskScope | None = None) -> Verdict:
    if role not in TRIGGER_ROLES:
        return Verdict("ok")
    m = RISKY_LABEL.search(label or "")
    if not m:
        return Verdict("ok")
    if scope is not None and scope.allows_label(label):
        return Verdict("ok")  # the user's request asked for exactly this ("...and submit it")
    return Verdict("confirm", f"clicking '{label}'")


RISKY_KEYS = {"cmd+backspace", "cmd+delete", "shift+cmd+backspace", "cmd+shift+backspace", "cmd+alt+backspace", "cmd+q"}


def classify_key(combo: str) -> Verdict:
    norm = combo.lower().replace("command", "cmd").replace("super", "cmd").replace(" ", "")
    if norm in RISKY_KEYS:
        return Verdict("confirm", f"key {combo} can delete or quit")
    return Verdict("ok")


class Aborted(Exception):
    pass


class Policy:
    """Turns verdicts into decisions according to the approval mode."""

    def __init__(self, mode: str = "risky", confirm: Callable[[str], bool] | None = None):
        self.mode = mode  # risky | all | none
        self.confirm = confirm
        self.approved: set[str] = set()
        self.scope = TaskScope()

    def check(self, verdict: Verdict, action: str) -> tuple[bool, str]:
        if verdict.level == "block":
            return False, f"Refused by safety policy: {verdict.reason}."
        needs = (verdict.level == "confirm" and self.mode != "none") or self.mode == "all"
        if not needs:
            return True, ""
        key = f"{action}|{verdict.reason}"
        if key in self.approved:
            return True, ""
        if self.confirm is None:
            return False, f"Not allowed without a human ({verdict.reason or action}). Find another way or report that you need the user."
        prompt = f"{action}" + (f"  — {verdict.reason}" if verdict.reason else "")
        if self.confirm(prompt):
            self.approved.add(key)
            return True, ""
        return False, "The user declined this action. Do not retry it; choose another approach or stop."


def kill_switch_engaged(pos: tuple[float, float]) -> bool:
    return pos[0] <= 2 and pos[1] <= 2
