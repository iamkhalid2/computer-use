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
    r"confirm payment|subscribe|unsubscribe|wipe|reset|discard|don.t save|replace|force quit|quit)\b",
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
    "basename", "dirname", "realpath", "test", "true", "env", "printenv", "osascript", "sleep",
}
WRITE_FLAGS = {"find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}, "sed": {"-i"},
               "sort": {"-o", "--output"}, "textutil": {"-output"}, "defaults": {"write", "delete", "import"},
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
               "replace", "discard", "reset", "transfer", "pay", "buy", "purchase", "checkout", "sign out", "log out",
               "kill", "terminate", "quit", "force quit"]
# A verb also authorises the buttons that carry it out under another name ("buy" -> "Place order").
IMPLIES = {"buy": ["place order", "complete order", "complete purchase", "buy now", "pay", "purchase", "checkout", "check out"],
           "kill": ["quit", "force quit"], "terminate": ["quit", "force quit"]}
IMPLIES["purchase"] = IMPLIES["buy"]
KILL_VERBS = {"kill", "terminate", "quit", "force quit"}
KILLERS = {"kill", "pkill", "killall"}
FILE_WRITERS = {"mkdir", "mv", "cp", "touch", "tee", "ln"}
FILE_DELETERS = {"rm", "rmdir"}
# Commands that run another command: judge the command they run, not the wrapper.
WRAPPERS = {"env", "nice", "nohup", "time", "command", "exec", "xargs", "caffeinate", "timeout", "gtimeout"}
# Opening these runs code (apps, scripts, installers), so it needs the same approval as running it.
EXECUTABLE_SUFFIXES = {".app", ".command", ".sh", ".tool", ".terminal", ".pkg", ".mpkg", ".dmg", ".scpt", ".scptd",
                       ".applescript", ".workflow", ".shortcut", ".jar", ".py", ".rb", ".pl", ".js", ".webloc", ".inetloc"}
# Link schemes that only show something; any other deep link can make an app act (run a shortcut, start a call).
VIEW_SCHEMES = {"http", "https", "mailto", "x-apple.systempreferences"}
APP_DIRS = ("/Applications/", "/System/Applications/", "/System/Library/CoreServices/", os.path.expanduser("~/Applications/"))
BULK = re.compile(r"\b(all|every|everything|entire)\b", re.I)
# "fill it in but don't submit it" names the verb to forbid it. A verb counts as asked for only where the words
# before it in the same clause do not negate it; when in doubt the button just asks first.
NEGATION = re.compile(r"(\bnot\b|n['’]t\b|\bdont\b|\bnever\b|\bwithout\b|\bno\b|\binstead of\b|\brather than\b|\bavoid\b|\bbefore you\b)", re.I)
CLAUSE_END = re.compile(r"[.;!?,\n]|\bbut\b|\bthen\b")


def asked_verbs(low: str) -> set[str]:
    """The risky verbs the request asks for: mentioned, and not only in order to rule them out."""
    out = set()
    for v in SCOPE_VERBS:
        for m in re.finditer(rf"\b{re.escape(v)}\b", low):
            lead = low[max(0, m.start() - 48):m.start()]
            clause = CLAUSE_END.split(lead)[-1]
            if not NEGATION.search(clause):
                out.add(v)
                break
    return out
CURL_WRITE_FLAGS = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "-F", "--form", "-T", "--upload-file",
                    "-X", "--request", "-o", "--output", "-O", "--remote-name", "-K", "--config"}


@dataclass
class TaskScope:
    """What the user's own request authorises. Built only from the user's words (and the
    operator's configured roots), never from screen content, so injected text can't widen it."""

    origins: set[str] = field(default_factory=set)  # scheme://host:port of URLs in the task
    roots: list[Path] = field(default_factory=list)  # folders the task names (or configured write roots)
    verbs: set[str] = field(default_factory=set)
    bulk: bool = False  # the request itself says "all" or "every": "Delete" never authorises "Delete all" otherwise
    text: str = ""  # the request, lowercased: a process may be stopped only if the user named it

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
        return cls(origins, roots, asked_verbs(low), bool(BULK.search(low)), low)

    def add_places(self, text: str) -> None:
        """Widen where Pilot may work with the folders and sites in something the user typed when asked.
        Places only: an answer never adds a verb, so nothing said in passing can authorise a send, a purchase or a delete."""
        more = TaskScope.from_task(text)
        self.origins |= more.origins
        self.roots += [r for r in more.roots if r not in self.roots]

    def allows_url(self, url: str) -> bool:
        u = urlparse(url)
        return f"{u.scheme}://{u.netloc}" in self.origins

    def allows_path(self, raw: str) -> bool:
        path = Path(os.path.expanduser(raw)).resolve()
        return any(path == r or r in path.parents for r in self.roots)

    def allows_label(self, label: str) -> bool:
        low = (label or "").lower()
        if BULK.search(low) and not self.bulk:
            return False
        allowed = self.verbs | {i for v in self.verbs for i in IMPLIES.get(v, [])}
        return any(re.search(rf"\b{re.escape(v)}\b", low) for v in allowed)

    def names(self, word: str) -> bool:
        """The user's request names this process (a whole word of 3+ characters, not a number)."""
        w = word.lower().rsplit("/", 1)[-1]
        return len(w) >= 3 and not w.isdigit() and re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", self.text) is not None

    def allows_kill(self, cmd: str, words: list[str]) -> bool:
        """`kill`, `pkill` or `killall` aimed only at processes the user asked to stop, by name."""
        if not self.verbs & KILL_VERBS:
            return False
        targets, skip = [], False
        for w in words[1:]:
            if skip:
                skip = False
            elif w in {"-s", "-n", "-u", "-U", "-g", "-G", "-t"}:
                skip = True  # takes a value (signal, user, group, tty)
            elif not w.startswith("-"):
                targets.append(w)
        if not targets:
            return False
        if cmd == "kill":
            return all(t.isdigit() and int(t) > 1 and self.names_process(int(t)) for t in targets)
        return all(self.names(t) for t in targets)

    def names_process(self, pid: int) -> bool:
        import subprocess

        ps = subprocess.run(["ps", "-p", str(pid), "-o", "uid=,args="], capture_output=True, text=True).stdout.split()
        return len(ps) >= 2 and ps[0] == str(os.getuid()) and any(self.names(w) for w in ps[1:])


def split_commands(line: str) -> list[str] | None:
    """The separate commands in a shell line: split at ; | & && || and at line breaks, but only outside quotes.
    None when a quote never closes.

    A line break starts a new command exactly as `;` does, so each line is judged on its own (`ls` on line one
    must not vouch for `rm` on line two). An operator inside quotes is just text (`stat -f "%Sm|%N"`)."""
    out: list[str] = []
    cur: list[str] = []
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                i += 1
                cur.append(line[i])
            elif ch == quote:
                quote = ""
        elif ch == "\\" and i + 1 < len(line):
            i += 1
            cur += [" "] if line[i] == "\n" else [ch, line[i]]  # a backslash at a line's end joins the lines
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch in ";|&\n":
            out.append("".join(cur))
            cur = []
            if ch in "|&" and line[i + 1:i + 2] == ch:
                i += 1
        else:
            cur.append(ch)
        i += 1
    if quote:
        return None
    return out + ["".join(cur)]


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
    segments = split_commands(stripped)
    if segments is None:
        return Verdict("confirm", "unparseable shell command")
    for segment in segments:
        v, cwd = _segment_verdict(segment, scope, cwd)
        if v.level != "ok":
            return v
    return Verdict("ok")


def _scoped_find_delete(words: list[str], scope: TaskScope, cwd: str | None) -> bool:
    """`find <roots> ... -delete` whose only write is -delete and whose roots all lie in a folder the user named."""
    if {w for w in words if w in WRITE_FLAGS["find"]} != {"-delete"}:
        return False
    roots = []
    for w in words[1:]:
        if w.startswith(("-", "(", "!")):
            break
        roots.append(w)
    return all(_in_scope(r, scope, cwd) for r in (roots or ["."]))


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
    if cmd in WRAPPERS:
        rest = words[1:]
        if cmd == "command" and {"-v", "-V"} & set(rest):
            return Verdict("ok"), cwd  # a lookup, runs nothing
        while rest and (rest[0].startswith("-") or (cmd == "env" and "=" in rest[0]) or re.fullmatch(r"[\d.]+[smhd]?", rest[0])):
            rest = rest[1:]
        if not rest:
            return Verdict("ok" if cmd in {"env", "time"} else "confirm", f"`{cmd}` with nothing to run"), cwd
        return _segment_verdict(shlex.join(rest), scope, cwd)
    args = [w for w in words[1:] if not w.startswith("-")]
    if cmd == "open":
        targets = [a for i, a in enumerate(words[1:], 1) if not a.startswith("-") and words[i - 1] not in {"-a", "-b"}]
        for t in targets:
            if (v := classify_open(t, scope)).level != "ok":
                return v, cwd
        return Verdict("ok"), cwd
    if cmd == "awk" and any("system(" in w for w in words[1:]):
        return Verdict("confirm", "`awk` running a command"), cwd
    if cmd == "textutil" and {"-convert", "-cat"} & set(words) and "-stdout" not in words:
        return Verdict("confirm", "`textutil` writing a file"), cwd
    if cmd == "cd":
        return Verdict("ok"), (args[0] if args else cwd)
    if scope is not None and cmd in {"curl", "wget"}:
        urls = [a for a in args if a.startswith(("http://", "https://"))]
        if urls and all(scope.allows_url(u) for u in urls) and not CURL_WRITE_FLAGS & set(words[1:]):
            return Verdict("ok"), cwd  # reading a page the user pointed at
    if scope is not None and cmd in KILLERS and scope.allows_kill(cmd, words):
        return Verdict("ok"), cwd  # stopping the process the user named
    if scope is not None and cmd == "find" and scope.verbs & {"delete", "remove"} and _scoped_find_delete(words, scope, cwd):
        return Verdict("ok"), cwd  # deleting inside a folder the user named, as asked
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


def classify_open(target: str, scope: TaskScope | None = None) -> Verdict:
    """Opening a page or a document only shows it; opening an app bundle, a script or an installer runs code,
    and a deep link can make an app act, so those need the user's say-so."""
    t = target.strip()
    m = re.match(r"^([a-z][a-z0-9+.-]*):", t, re.I)
    if m and not re.match(r"^[a-z]:[\\/]", t, re.I):
        if m.group(1).lower() in VIEW_SCHEMES or (scope is not None and scope.allows_url(t)):
            return Verdict("ok")
        return Verdict("confirm", f"opening a {m.group(1)}: link can make an app act")
    path = Path(os.path.expanduser(t))
    if path.suffix.lower() == ".app" and any(str(path.resolve()).startswith(d) for d in APP_DIRS):
        return Verdict("ok")  # launching an installed app
    if path.suffix.lower() in EXECUTABLE_SUFFIXES or (path.is_file() and os.access(path, os.X_OK)):
        return Verdict("confirm", f"opening {path.name} runs it")
    return Verdict("ok")


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
