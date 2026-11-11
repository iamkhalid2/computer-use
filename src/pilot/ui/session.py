"""One Pilot window's worth of state: the task that is running, what it has said so far, and
what it is waiting to hear from the user.

The agent already talks to people through one object, the Reporter. `WindowReporter` is that
object for a window instead of a terminal: every line becomes an event the page draws, and
every question blocks the agent's thread until the page posts an answer. Nothing else about
the agent changes, so a task behaves the same from the window as from the command line.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..console import Reporter
from ..safety import Aborted

STOPPED = "stopped from the Pilot window"

# Tool calls in words a person who has never seen a terminal can follow.
_CLICKS = {"left_click": "Clicked", "right_click": "Right-clicked", "double_click": "Double-clicked", "triple_click": "Selected a line",
           "middle_click": "Clicked", "left_click_drag": "Dragged", "mouse_move": "Moved the pointer", "scroll": "Scrolled",
           "screenshot": "Looked at the screen", "zoom": "Looked closer", "wait": "Waited", "cursor_position": "Checked the pointer"}
_UI = {"press": "Pressed", "set_value": "Filled in", "focus": "Selected", "show_menu": "Opened the menu of", "increment": "Raised",
       "decrement": "Lowered", "confirm": "Confirmed", "cancel": "Cancelled", "raise": "Brought forward", "scroll_to_visible": "Scrolled to"}
_WEB = {"goto": "Opened", "click": "Clicked", "fill": "Filled in", "select": "Chose", "press": "Pressed", "type": "Typed", "scroll": "Scrolled",
        "drag": "Dragged", "back": "Went back", "screenshot": "Looked at the page", "snapshot": "Read the page", "dialog": "Answered a dialog"}


def _short(value: Any, n: int = 60) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def plain(name: str, args: dict[str, Any]) -> tuple[str, str]:
    """(what happened in plain words, technical detail for the curious)."""
    a = args or {}
    if name == "computer":
        act = str(a.get("action", ""))
        if act == "type":
            return f"Typed “{_short(a.get('text', ''), 50)}”", ""
        if act in {"key", "hold_key"}:
            return f"Pressed {_short(a.get('text', ''), 30)}", ""
        return _CLICKS.get(act, act.replace("_", " ").capitalize() or "Used the mouse"), ""
    if name == "ui":
        what = a.get("target") or "a control"
        verb = _UI.get(str(a.get("action") or "press"), "Used")
        return f"{verb} {_short(what, 40)}" + (f": “{_short(a['value'], 40)}”" if a.get("value") else ""), ""
    if name == "open":
        return f"Opened {_short(a.get('target', ''), 60)}", ""
    if name == "shell":
        return "Ran a command", _short(a.get("command", ""), 400)
    if name == "applescript":
        return "Ran a script", _short(a.get("script", ""), 400)
    if name == "write_file":
        return f"Saved {_short(a.get('path', ''), 60)}", ""
    if name == "web":
        act = str(a.get("action", ""))
        return f"{_WEB.get(act, 'Used the page')} {_short(a.get('url') or a.get('text') or a.get('option') or '', 60)}".strip(), ""
    if name == "menu":
        return "Used the menu " + " › ".join(str(p) for p in a.get("path") or []), ""
    if name == "find":
        return f"Looked for “{_short(a.get('query', ''), 40)}”", ""
    if name == "wait_for":
        return f"Waited for {_short(a.get('text') or a.get('app') or 'the screen', 40)}", ""
    if name == "ask_user":
        return "", ""  # the question itself is on screen
    return name.replace("_", " ").capitalize(), ""


class WindowReporter(Reporter):
    """The agent's voice in a window. Printing still happens, so the log file reads like a terminal session."""

    def __init__(self, session: "Session"):
        super().__init__()
        self.session = session

    def _check(self) -> None:
        if self.session.stop.is_set():
            raise Aborted(STOPPED)

    # -- lines ---------------------------------------------------------------------
    def header(self, task: str, settings: Any) -> None:
        super().header(task, settings)

    def plan(self, data: dict[str, Any], seconds: float, label: str = "Plan") -> None:
        super().plan(data, seconds, label)
        steps = [str(s.get("goal")) for s in data.get("steps", []) if isinstance(s, dict) and s.get("goal")]
        self.session.emit("plan", label=label, note=str(data.get("strategy") or data.get("advice") or "")[:600], steps=steps)

    def step(self, i: int, n: int, goal: str) -> None:
        super().step(i, n, goal)
        self.session.emit("step", index=i, total=n, goal=goal)

    def model(self, role: str, latency: float, text: str = "") -> None:
        super().model(role, latency, text)
        self._check()  # between the model's decision and its actions: Stop takes effect before anything more is done

    def note(self, text: str) -> None:
        super().note(text)
        if text.strip():
            self.session.emit("note", text=text.strip()[:300])

    def action(self, name: str, args: dict[str, Any], ok: bool, result: str, seconds: float) -> None:
        super().action(name, args, ok, result, seconds)
        text, detail = plain(name, args)
        if text:
            self.session.emit("action", text=text, ok=ok, detail=detail, error="" if ok else _short(result, 240))
        self._check()

    def info(self, text: str) -> None:
        super().info(text)
        low = text.lstrip("↑ ").lower()
        if low.startswith("verify"):  # the terminal's shorthand, in words
            text = "Checked the result: " + ("all good." if "incomplete" not in low else "not there yet, carrying on.")
        elif low.startswith("escalate"):
            text = "That didn't work. Rethinking the approach…"
        elif low.startswith(("learned in", "learning skipped")):
            return
        self.session.emit("info", text=text[:400])

    def route(self, goal: str, steps: list[str]) -> None:
        super().route(goal, steps)
        self.session.emit("route", goal=goal, steps=steps)

    def lead(self, why: dict[str, Any]) -> None:
        super().lead(why)
        self.session.emit("info", text="I've done this kind of job before: taking the quick route." if why.get("lead") == "executor"
                          else "This is a new kind of job for me: thinking it through first. It will be quicker next time.")

    def final(self, success: bool, answer: str, summary: str, stats: Any, seconds: float, report: str) -> None:
        super().final(success, answer, summary, stats, seconds, report)
        self.session.result = {"success": success, "answer": answer, "summary": summary, "seconds": round(seconds, 1),
                               "actions": stats.actions, "report": report}

    # -- questions: block the agent's thread until the page answers ---------------------
    def ask(self, question: str, default: str = "") -> str:
        answer = self.session.wait_for("ask", question=question, default=default)
        return str(answer or "").strip() or default

    def confirm(self, prompt: str) -> bool:
        return bool(self.session.wait_for("confirm", prompt=prompt))

    def keep_going(self, what: str, progress: str = "") -> bool:
        return bool(self.session.wait_for("keep_going", what=what))


class Session:
    """Events for the page, one task at a time. Thread-safe: the agent, the HTTP handlers and the window all touch it."""

    def __init__(self, make_agent: Callable[["Session"], Any] | None = None, on_state: Callable[[str], None] | None = None):
        self.cv = threading.Condition()
        self.events: list[dict[str, Any]] = []
        self.state = "idle"  # idle | working | asking | done
        self.stop = threading.Event()
        self.pending: dict[str, Any] | None = None  # the question on screen: {"id", "kind", "answer", "answered"}
        self.result: dict[str, Any] | None = None
        self.last_run: Path | None = None
        self.history: list[str] = []
        self.thread: threading.Thread | None = None
        self.make_agent = make_agent or default_agent
        self.on_state = on_state
        self.reporter = WindowReporter(self)
        self._agent: Any = None
        self._qid = 0

    # -- events --------------------------------------------------------------------
    def emit(self, kind: str, /, **data: Any) -> dict[str, Any]:
        with self.cv:
            ev = {"seq": len(self.events) + 1, "type": kind, "at": round(time.time(), 2), **data}
            self.events.append(ev)
            self.cv.notify_all()
        return ev

    def events_after(self, seq: int, timeout: float = 25.0) -> list[dict[str, Any]]:
        """Events newer than `seq`, waiting up to `timeout` seconds for the first one (a long poll)."""
        with self.cv:
            self.cv.wait_for(lambda: len(self.events) > seq, timeout=timeout)
            return self.events[max(seq, 0):]

    def _set(self, state: str) -> None:
        self.state = state
        self.emit("state", state=state)
        if self.on_state:
            try:
                self.on_state(state)
            except Exception:  # noqa: BLE001 - the window is cosmetic; the task goes on
                pass

    # -- the user's side -------------------------------------------------------------
    def submit(self, task: str) -> bool:
        task = " ".join(task.split())
        with self.cv:
            if not task or self.state in {"working", "asking"}:
                return False
            self.state = "working"  # claimed under the lock: two clicks start one task
        self.stop.clear()
        self.result, self.pending = None, None
        self.emit("start", task=task)
        self._set("working")
        self.thread = threading.Thread(target=self._run, args=(task,), name="pilot-task", daemon=True)
        self.thread.start()
        return True

    def answer(self, qid: str, value: Any) -> bool:
        with self.cv:
            p = self.pending
            if p is None or p["id"] != qid or p["answered"]:
                return False
            p["answer"], p["answered"] = value, True
            self.cv.notify_all()
        return True

    def request_stop(self) -> None:
        self.stop.set()
        self.emit("stopping")
        with self.cv:
            self.cv.notify_all()

    def rate(self, rating: str, note: str = "") -> bool:
        from .. import feedback

        if self.last_run is None or rating not in {feedback.UP, feedback.DOWN}:
            return False
        feedback.record(self.last_run, rating, note, source="window")
        skills = getattr(self._agent, "skills", None)
        if skills is not None and skills.mode == "learn" and skills.last.get("run") == self.last_run.name:
            skills.rate(rating == feedback.UP)  # your verdict outweighs Pilot's own: it moves the playbook's belief
        self.emit("rated", rating=rating)
        return True

    # -- the agent's side ------------------------------------------------------------
    def wait_for(self, kind: str, **data: Any) -> Any:
        """Put a question on screen and wait for its answer. Stop while waiting ends the run."""
        with self.cv:
            self._qid += 1
            qid = f"q{self._qid}"
            self.pending = {"id": qid, "kind": kind, "answer": None, "answered": False}
        self.emit(kind, id=qid, **data)
        self._set("asking")
        with self.cv:
            self.cv.wait_for(lambda: self.pending["answered"] or self.stop.is_set())
            p, self.pending = self.pending, None
        if not p["answered"]:
            raise Aborted(STOPPED)
        self.emit("answered", id=qid, kind=kind, value=p["answer"])
        self._set("working")
        return p["answer"]

    def _run(self, task: str) -> None:
        from ..mac.apps import KeepAwake

        try:
            if self._agent is None:
                self._agent = self.make_agent(self)
            with KeepAwake():
                out = self._agent.run(task, context=" | ".join(self.history[-4:]))
            self.last_run = Path(out.report).parent if out.report else None
            self.history.append(f"{task} → {'done' if out.success else 'not done'}{': ' + out.answer[:120] if out.answer else ''}")
            res = self.result or {}
            stopped = self.stop.is_set()
            worked = max(out.seconds - float(getattr(self._agent, "waited", 0.0) or 0.0), 0.0)  # not the time spent waiting for answers
            self.emit("done", success=out.success, stopped=stopped, answer=out.answer, summary=out.summary,
                      reason="" if out.success else ("You stopped it." if stopped else out.reason),
                      seconds=round(worked, 1), actions=res.get("actions", 0), report=out.report)
        except Exception as e:  # noqa: BLE001 - whatever went wrong, the window must say so and be usable again
            self.emit("done", success=False, stopped=False, answer="", summary="", reason=f"{type(e).__name__}: {e}"[:600], seconds=0, actions=0, report="")
        finally:
            self.pending = None
            self._set("done")


def default_agent(session: Session) -> Any:
    """The same agent `pilot "task"` builds, speaking through the window."""
    from ..agent.runner import Agent
    from ..config import build_settings
    from ..env import MacEnv
    from ..providers.jev import Jev

    settings = build_settings(interactive=True, playbooks="learn")
    env = MacEnv(settings.profile.max_long_edge, settings.profile.image_format, settings.profile.use_ax)
    env.host_pid = os.getpid()  # Pilot never clicks or types into its own window
    return Agent(settings, env, session.reporter, Jev())


def readiness() -> dict[str, Any]:
    """What a first-time user still has to switch on, read fresh each time (no model calls)."""
    from ..config import default_preset, has_credentials, PRESETS
    from ..mac.ax import AX
    from ..mac.screen import Screen

    preset = os.environ.get("PILOT_PRESET") or default_preset()
    specs = [s for s in PRESETS.get(preset, PRESETS["sub"]).values() if s]
    try:
        model = all(has_credentials(s.provider) for s in specs)
    except Exception:  # noqa: BLE001
        model = False
    if not model:
        from .. import config

        config._CC_LOGGED_IN = None  # not signed in a moment ago: look again next time instead of remembering it
    try:
        control = bool(AX().trusted())
    except Exception:  # noqa: BLE001
        control = False
    try:
        screen = bool(Screen().has_permission())
    except Exception:  # noqa: BLE001
        screen = False
    return {"control": control, "screen": screen, "model": model, "preset": preset, "ready": control and screen and model}
