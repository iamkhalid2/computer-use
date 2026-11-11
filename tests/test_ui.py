"""The Pilot window's back end: the session that carries questions and answers, and the server that guards it."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from pilot.safety import Aborted
from pilot.ui import server as server_mod
from pilot.ui.server import UIServer
from pilot.ui.session import Session, plain


class FakeAgent:
    """Speaks through the reporter exactly as the real agent does, with no models and no Mac."""

    memory = None

    def __init__(self, session, script):
        self.r, self.script = session.reporter, script

    def run(self, task, context=""):
        stats = SimpleNamespace(actions=2, cost=0, cost_known=False, model_calls=1, model_seconds=0.1)
        try:
            answer = self.script(self.r, task)
        except Aborted as e:
            self.r.final(False, "", f"aborted: {e}", stats, 0.1, "")
            return SimpleNamespace(success=False, answer="", summary="", reason=f"aborted: {e}", seconds=0.1, report="")
        self.r.final(True, answer, "did it", stats, 0.1, "")
        return SimpleNamespace(success=True, answer=answer, summary="did it", reason="", seconds=0.1, report="")


def session_with(script):
    return Session(make_agent=lambda s: FakeAgent(s, script))


def wait_event(session, kind, after=0, timeout=5.0):
    seen = after
    for _ in range(200):
        for ev in session.events_after(seen, timeout=timeout / 20):
            seen = ev["seq"]
            if ev["type"] == kind:
                return ev
    raise AssertionError(f"no {kind!r} event; got {[e['type'] for e in session.events]}")


def test_a_question_blocks_the_agent_until_the_page_answers():
    def script(r, task):
        r.route("Tidy your folder.", ["Look", "Show you the plan and wait for your yes", "Move"])
        where = r.ask("Which folder?", "")
        how = r.ask("Sort how?", "By type")
        r.step(1, 3, "Look")
        r.note("Listing files")
        r.action("shell", {"command": "ls ~/x"}, True, "a b", 0.1)
        ok = r.confirm("shell: mv a b  — `mv` can change things")
        return f"{where}|{how}|{ok}"

    s = session_with(script)
    assert s.submit("organise my drive") and not s.submit("a second task while busy")
    q1 = wait_event(s, "ask")
    assert q1["question"] == "Which folder?" and s.state == "asking"
    assert not s.answer("wrong-id", "x") and s.answer(q1["id"], "Downloads") and not s.answer(q1["id"], "again")
    q2 = wait_event(s, "ask", after=q1["seq"])
    assert q2["default"] == "By type" and s.answer(q2["id"], "")  # Return on an empty box accepts the suggestion
    c = wait_event(s, "confirm", after=q2["seq"])
    assert s.answer(c["id"], False)
    done = wait_event(s, "done", after=c["seq"])
    assert done["success"] and done["answer"] == "Downloads|By type|False"
    kinds = [e["type"] for e in s.events]
    assert kinds[0] == "start" and "route" in kinds and "step" in kinds and "note" in kinds and kinds[-1] == "state" and s.state == "done"
    act = next(e for e in s.events if e["type"] == "action")
    assert act["text"] == "Ran a command" and act["detail"] == "ls ~/x"  # plain words first, the command for the curious
    assert s.submit("another task")  # usable again once done


def test_stop_ends_a_run_even_while_it_waits_for_an_answer():
    def script(r, task):
        r.ask("Where to?", "")
        raise AssertionError("must not continue after Stop")

    s = session_with(script)
    s.submit("book a flight")
    wait_event(s, "ask")
    s.request_stop()
    done = wait_event(s, "done")
    assert not done["success"] and done["stopped"] and done["reason"] == "You stopped it."


def test_stop_lands_before_the_next_action():
    gate = threading.Event()

    def script(r, task):
        gate.wait(5)
        r.model("executor", 0.1)  # the reporter is told after every model call: Stop is checked here
        raise AssertionError("an action ran after Stop")

    s = session_with(script)
    s.submit("do things")
    s.request_stop()
    gate.set()
    done = wait_event(s, "done")
    assert done["stopped"] and not done["success"] and done["reason"] == "You stopped it."


def test_a_crash_is_reported_and_the_window_stays_usable():
    def script(r, task):
        raise RuntimeError("provider exploded")

    s = session_with(script)
    s.submit("anything")
    done = wait_event(s, "done")
    assert not done["success"] and "provider exploded" in done["reason"] and s.state == "done" and s.submit("again")


def test_actions_read_as_plain_words():
    assert plain("computer", {"action": "type", "text": "hello"})[0] == "Typed “hello”"
    assert plain("computer", {"action": "key", "text": "cmd+s"})[0] == "Pressed cmd+s"
    assert plain("ui", {"target": "Save", "action": "press"})[0] == "Pressed Save"
    assert plain("ui", {"target": "Name", "action": "set_value", "value": "Ada"})[0] == "Filled in Name: “Ada”"
    assert plain("open", {"target": "Safari"})[0] == "Opened Safari"
    assert plain("write_file", {"path": "~/a.txt", "text": "x"})[0] == "Saved ~/a.txt"
    assert plain("shell", {"command": "ls -la"}) == ("Ran a command", "ls -la")
    assert plain("ask_user", {"question": "?"})[0] == ""  # the question is already on screen


# -- the server ---------------------------------------------------------------------------
@pytest.fixture
def ui(monkeypatch):
    monkeypatch.setattr(server_mod, "readiness", lambda: {"control": True, "screen": True, "model": True, "preset": "sub", "ready": True})
    srv = UIServer(session_with(lambda r, task: r.ask("Which one?", "")), port=0).start()
    yield srv
    srv.httpd.shutdown()


def call(ui, path, body=None, token=None, host=None, timeout=5):
    req = urllib.request.Request(ui.url.rstrip("/") + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Pilot-Token", token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_only_pilots_own_page_can_drive_it(ui):
    status, page, headers = call(ui, "/")
    assert status == 200 and ui.token in page and "__PILOT_TOKEN__" not in page
    assert not any(h.lower().startswith("access-control") for h in headers)  # no other page may read replies
    assert call(ui, "/api/state")[0] == 403  # no secret
    assert call(ui, "/api/task", {"task": "delete everything"})[0] == 403
    assert call(ui, "/api/task", {"task": "x"}, token="guess")[0] == 403
    assert call(ui, "/", host="evil.example")[0] == 403  # a domain rebound to this Mac
    assert call(ui, "/api/state", token=ui.token, host=f"evil.example:{ui.port}")[0] == 403
    assert ui.session.events == []  # none of that started anything
    assert ui.httpd.server_address[0] == "127.0.0.1"


def test_a_task_runs_through_the_endpoints(ui):
    t = ui.token
    assert json.loads(call(ui, "/api/state", token=t)[1])["ready"] is True
    assert call(ui, "/api/task", {"task": "  "}, token=t)[0] == 409
    assert call(ui, "/api/task", {"task": "open the file"}, token=t)[0] == 200
    assert call(ui, "/api/task", {"task": "another"}, token=t)[0] == 409  # one at a time
    q = wait_event(ui.session, "ask")
    assert call(ui, "/api/answer", {"id": q["id"], "value": "the blue one"}, token=t)[0] == 200
    wait_event(ui.session, "done")
    events = json.loads(call(ui, "/api/events?after=0&wait=1", token=t)[1])["events"]
    assert [e["type"] for e in events][:1] == ["start"] and events[-1] == {**events[-1], "type": "state", "state": "done"}
    assert next(e for e in events if e["type"] == "done")["answer"] == "the blue one"
    assert json.loads(call(ui, f"/api/events?after={events[-1]['seq']}&wait=0.2", token=t)[1])["events"] == []  # long poll times out empty
    assert call(ui, "/api/nope", {}, token=t)[0] == 404
    assert call(ui, "/api/task", None, token=t)[0] == 404  # GET on a POST route


def test_pilot_app_is_a_signed_bundle_that_starts_this_checkout(tmp_path):
    import plistlib
    import shutil
    import subprocess

    from pilot.ui import bundle

    if not shutil.which("cc"):
        pytest.skip("no C compiler")
    app = bundle.build(tmp_path, repo=tmp_path / "repo with space", python=tmp_path / "py")
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == bundle.BUNDLE_ID and info["CFBundleExecutable"] == "Pilot" and info["LSUIElement"] is True
    assert "NSAppleEventsUsageDescription" in info  # without it macOS silently refuses to let Pilot script other apps
    exe = app / "Contents" / "MacOS" / "Pilot"
    assert exe.stat().st_mode & 0o111 and subprocess.run(["file", str(exe)], capture_output=True, text=True).stdout.count("Mach-O")
    assert subprocess.run([str(exe)], capture_output=True).returncode == 71  # the checkout it points at is missing: it stops, starts nothing
    assert bundle.build(tmp_path, repo=tmp_path, python=tmp_path / "py") == app  # rebuilding replaces its own app
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "Pilot.app").mkdir()
    with pytest.raises(SystemExit):  # never overwrites an app it did not make
        bundle.build(tmp_path / "other")
