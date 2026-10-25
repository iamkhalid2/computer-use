"""The agent loop end to end, against a fake Mac and scripted fake models."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pilot.agent import runner as runner_mod
from pilot.agent.runner import Agent
from pilot.config import ModelSpec, Settings
from pilot.console import Reporter
from pilot.mac.ax import Node
from pilot.mac.screen import Geometry
from pilot.providers.base import Provider, Session
from pilot.types import AssistantTurn, ImagePart, Observation, TextPart, ToolCall


class FakeInput:
    def __init__(self):
        self.log = []

    def position(self):
        return (500.0, 500.0)

    def click(self, x, y, button="left", count=1, modifiers=None):
        self.log.append(("click", round(x), round(y), button, count))

    def key(self, combo, repeat=1):
        self.log.append(("key", combo))

    def type_text(self, text):
        self.log.append(("type", text))
        return "typed"

    def move(self, x, y):
        self.log.append(("move", x, y))


class FakeAX:
    def __init__(self):
        self.performed = []

    def trusted(self, prompt=False):
        return True

    def element_at(self, x, y):
        return None

    def pid_of(self, el):
        return 7

    def attr(self, el, name):
        return "AXButton"

    def perform(self, el, action):
        self.performed.append((el, action))
        return 0

    def set_attr(self, el, name, value):
        return 0


class FakeEnv:
    def __init__(self, use_ax=False):
        self.geometry = Geometry.make(1000, 600, 2000, 1200, 1568)
        self.use_ax = use_ax
        self.registry = {}
        self.next_id = 1
        self.last_snapshot = None
        self.last_image = None
        self.host_pid = None
        self.input = FakeInput()
        self.ax = FakeAX()
        self.state = 0
        self.observations = 0

    def observe(self, settle=True, coord_space="pixels", with_ax=True):
        self.observations += 1
        h = f"h{self.state}"
        return Observation(ImagePart(b"img", "image/png", 1000, 600), app="TextEdit", outline="[1] button \"Save\" @130,110", hash=h)

    def to_points(self, x, y, space="pixels"):
        if space == "norm1000":
            x, y = x * self.geometry.model_w / 1000, y * self.geometry.model_h / 1000
        return self.geometry.model_to_points(x, y)

    def to_model(self, x, y, space="pixels"):
        return self.geometry.points_to_model(x, y)

    def image_part(self, img):
        return ImagePart(b"img", "image/png", 1000, 600)

    def frontmost(self):
        return (2, "TextEdit", None)


class FakeSession(Session):
    def __init__(self, script, log):
        self.script = script
        self.log = log

    def _next(self):
        turn = self.script.pop(0)
        return turn

    def send(self, parts):
        self.log.append(("send", parts))
        return self._next()

    def reply(self, calls, results, extra=None):
        self.log.append(("reply", calls, results, extra or []))
        return self._next()


class FakeProvider(Provider):
    def __init__(self, spec, sessions=None, json_script=None, native=False):
        self.spec = spec
        self.native_computer = native
        self.sessions = sessions or []
        self.json_script = json_script or []
        self.logs = []
        self.json_calls = []

    def session(self, system, tools):
        self.tools = tools
        log = []
        self.logs.append(log)
        return FakeSession(self.sessions.pop(0), log)

    def complete_json(self, system, parts, schema):
        self.json_calls.append((system, parts, schema))
        return self.json_script.pop(0), AssistantTurn(latency=0.01, model=self.spec.model)


def call(name, **args):
    call.n = getattr(call, "n", 0) + 1
    return ToolCall(f"c{call.n}", name, args)


def turn(*calls, text=""):
    return AssistantTurn(text=text, calls=list(calls), stop="tool_use" if calls else "end", latency=0.01, model="fake")


def make(monkeypatch, tmp_path, planner, executor, use_ax=False, **kw):
    providers = {"planner-model": planner, "executor-model": executor}
    monkeypatch.setattr(runner_mod, "get_provider", lambda spec, keep_images=3: providers[spec.model])
    monkeypatch.setattr(runner_mod.apps, "screen_locked", lambda: False)  # never depend on the real Mac's lock state
    from pilot.profile import KNOBS, Profile

    knobs = {"plan": "upfront", **{k: v for k, v in kw.items() if k in KNOBS}}
    rest = {k: v for k, v in kw.items() if k not in KNOBS}
    s = Settings(
        planner=ModelSpec("fake", "planner-model") if planner else None,
        executor=ModelSpec("fake", "executor-model"),
        interactive=False, approve="none", runs_dir=tmp_path, profile=Profile("test", knobs), **rest,
    )
    env = FakeEnv(use_ax=use_ax)
    agent = Agent(s, env, Reporter(quiet=True))
    return agent, env


PLAN2 = {"strategy": "two steps", "needs_answer": True, "steps": [
    {"goal": "open doc", "done_when": "doc open", "hint": ""},
    {"goal": "save", "done_when": "saved", "hint": "cmd+s"},
]}


def test_planner_executor_happy_path(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[
        [turn(call("computer", action="left_click", coordinate=[100, 50]), call("done", summary="opened", success=True))],
        [turn(call("computer", action="key", text="cmd+s")), turn(call("done", summary="saved", answer="42", success=True))],
    ])
    planner = FakeProvider(ModelSpec("fake", "planner-model"), json_script=[
        PLAN2, {"complete": True, "answer": "The answer is 42", "missing": "", "steps": []},
    ])
    agent, env = make(monkeypatch, tmp_path, planner, executor, use_ax=True)
    out = agent.run("do it")
    assert out.success and out.answer == "The answer is 42"
    assert ("click", 100, 50, "left", 1) in env.input.log
    assert ("key", "cmd+s") in env.input.log
    assert len(executor.logs) == 2  # a fresh session per step
    # step 2: after the key press the executor got a fresh observation attached to the result
    kind, calls, results, extra = executor.logs[1][1]
    assert kind == "reply" and results[0].image is not None and "Screen after your actions" in results[0].observation
    # the brief told the executor which step it is on
    first_send = executor.logs[1][0][1][0]
    assert isinstance(first_send, TextPart) and "Do step 2 now: save" in first_send.text
    assert {t.name for t in executor.tools} >= {"computer", "ui", "done", "escalate"}
    assert (tmp_path).glob("*/report.html")


def test_batch_stops_at_first_failure(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(call("computer", action="type"), call("computer", action="key", text="Return")),
        turn(call("done", summary="ok", success=True)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor)
    out = agent.run("type nothing")
    assert out.success
    _, calls, results, _ = executor.logs[0][1]
    assert results[0].ok is False and results[1].text.startswith("Not executed")
    assert ("key", "Return") not in env.input.log


def test_escalation_replans(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[
        [turn(call("escalate", reason="button missing"))],
        [turn(call("done", summary="used menu", success=True))],
    ])
    planner = FakeProvider(ModelSpec("fake", "planner-model"), json_script=[
        {"strategy": "click", "needs_answer": False, "steps": [{"goal": "click Export", "done_when": "dialog", "hint": ""}]},
        {"advice": "use the menu", "steps": [{"goal": "File > Export", "done_when": "dialog", "hint": "menu"}]},
        {"complete": True, "answer": "", "missing": "", "steps": []},
    ])
    agent, env = make(monkeypatch, tmp_path, planner, executor)
    out = agent.run("export")
    assert out.success and out.stats.escalations == 1
    assert "Planner advice: use the menu" in executor.logs[1][0][1][0].text


def test_verifier_rejects_and_adds_steps_once(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[
        [turn(call("done", summary="claimed", success=True))],
        [turn(call("done", summary="really", success=True))],
    ])
    planner = FakeProvider(ModelSpec("fake", "planner-model"), json_script=[
        {"strategy": "", "needs_answer": False, "steps": [{"goal": "a", "done_when": "x", "hint": ""}]},
        {"complete": False, "answer": "", "missing": "not saved", "steps": [{"goal": "save it", "done_when": "saved", "hint": ""}]},
        {"complete": False, "answer": "", "missing": "still not saved", "steps": [{"goal": "again", "done_when": "", "hint": ""}]},
    ])
    agent, env = make(monkeypatch, tmp_path, planner, executor)
    out = agent.run("save")
    assert not out.success and "still not saved" in out.reason


def test_text_only_end_is_nudged_then_accepted(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[turn(text="I think it's done"), turn(text="All done.")]])
    agent, env = make(monkeypatch, tmp_path, None, executor)
    out = agent.run("x")
    assert out.success and executor.logs[0][1][0] == "send"


def test_loop_detection_and_no_change_notes(monkeypatch, tmp_path):
    same = lambda: call("computer", action="left_click", coordinate=[10, 10])  # noqa: E731
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(same()), turn(same()), turn(same()), turn(call("done", summary="gave up", success=False)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor)
    out = agent.run("click")
    replies = [e for e in executor.logs[0] if e[0] == "reply"]
    texts = [p.text for _, _, _, extra in replies for p in extra if isinstance(p, TextPart)]
    assert any("did not visibly change" in t for t in texts)
    # identical call args differ only in id; the signature ignores ids
    assert any("repeated the same actions" in t for t in texts)
    assert not out.success


def test_turn_budget(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(call("computer", action="key", text="a")) for _ in range(5)
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor, max_turns=2)
    out = agent.run("x")
    assert not out.success and "executor turns" in out.reason


def test_ui_tool_presses_by_label(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(call("ui", target="Save")), turn(call("done", summary="saved", success=True)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor, use_ax=True)
    ref = SimpleNamespace(name="save-ref")
    node = Node("AXButton", label="Save", frame=(100, 100, 60, 20), ref=ref, id=1)
    env.registry = {1: node}
    env.next_id = 2
    out = agent.run("save")
    assert out.success and (ref, "AXPress") in env.ax.performed


def test_norm1000_coordinates(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(call("computer", action="left_click", coordinate=[500, 500])), turn(call("done", summary="ok", success=True)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor)
    agent.toolbox.space = "norm1000"
    agent.run("click centre")
    assert ("click", 500, 300, "left", 1) in env.input.log  # 1000x600 screen centre


@pytest.mark.parametrize("label", ["Send", "Delete"])
def test_risky_click_refused_without_human(monkeypatch, tmp_path, label):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(call("ui", target=label)), turn(call("done", summary="blocked", success=False)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor, use_ax=True)
    agent.policy.mode = "risky"
    env.registry = {1: Node("AXButton", label=label, frame=(0, 0, 10, 10), ref=object(), id=1)}
    agent.run("press it")
    _, _, results, _ = executor.logs[0][1]
    assert results[0].ok is False and "without a human" in results[0].text
    assert env.ax.performed == []


def test_no_accessibility_offers_only_pixel_tools(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[turn(call("done", summary="ok", success=True))]])
    agent, env = make(monkeypatch, tmp_path, None, executor, use_ax=False)
    agent.run("x")
    names = {t.name for t in executor.tools}
    assert {"computer", "open", "done"} <= names and not names & {"ui", "find", "menu", "wait_for"}


def test_lazy_plan_skips_planner_on_easy_task(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[turn(call("done", summary="quick", success=True))]])
    planner = FakeProvider(ModelSpec("fake", "planner-model"), json_script=[])
    agent, env = make(monkeypatch, tmp_path, planner, executor, plan="lazy")
    out = agent.run("open calculator")
    assert out.success and planner.json_calls == []  # no planner latency at all
    assert "Do the whole task" in executor.logs[0][0][1][0].text


def test_lazy_plan_calls_planner_when_stuck(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[
        [turn(call("escalate", reason="no idea where Export lives"))],
        [turn(call("done", summary="exported", success=True))],
    ])
    planner = FakeProvider(ModelSpec("fake", "planner-model"), json_script=[
        {"advice": "use File > Export", "steps": [{"goal": "File > Export as PDF", "done_when": "saved", "hint": "menu"}]},
        {"complete": True, "answer": "", "missing": "", "steps": []},
    ])
    agent, env = make(monkeypatch, tmp_path, planner, executor, plan="lazy")
    out = agent.run("export to pdf")
    assert out.success and len(planner.json_calls) == 2  # replan + verify (verify because it escalated)


def test_write_file_inside_workspace_needs_no_human(monkeypatch, tmp_path):
    target = tmp_path / "ws" / "out" / "total.txt"
    outside = tmp_path / "elsewhere.txt"
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(call("write_file", path=str(target), text="196.48"), call("write_file", path=str(outside), text="x")),
        turn(call("done", summary="written", success=True)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor, write_roots=[str(tmp_path / "ws")])
    agent.policy.mode = "risky"
    agent.run("write the total")
    _, _, results, _ = executor.logs[0][1]
    assert target.read_text() == "196.48" and results[0].ok
    assert results[1].ok is False and not outside.exists()  # outside the workspace: needs a human, none available


def test_consent_dialogs_are_left_to_the_user(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(call("computer", action="left_click", coordinate=[10, 10])), turn(call("done", summary="left it", success=False)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor)
    agent.policy.mode = "risky"
    env.frontmost = lambda: (9, "UserNotificationCenter", None)
    agent.run("allow it")
    _, _, results, _ = executor.logs[0][1]
    assert results[0].ok is False and "dialog" in results[0].text and env.input.log == []


def test_done_without_outcome_is_not_success(monkeypatch, tmp_path):
    # Seen live: a correct "no such setting exists" summary with no flag was scored as success.
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(ToolCall("x1", "done", {"summary": "macOS has no setting for the system font"})),
        turn(ToolCall("x2", "done", {"summary": "macOS has no setting for the system font", "success": False})),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor)
    out = agent.run("change the system font to Comic Sans")
    _, _, results, _ = executor.logs[0][1]
    assert results[0].ok is False and "success" in results[0].text
    assert out.success is False and "no setting" in out.reason


def test_evidence_gate_catches_uncommitted_edit(monkeypatch, tmp_path):
    # Seen live: Finder's rename field showed "Pilot Folder" but no folder existed on disk.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "PilotEval").mkdir()
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(call("done", summary="the list shows Pilot Folder", success=True)),
        turn(call("done", summary="disk shows it is missing", success=False)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor)
    out = agent.run("In Finder, create a folder named 'Pilot Folder' inside ~/PilotEval.")
    _, _, results, _ = executor.logs[0][1]
    assert results[0].ok is False and "Read from disk" in results[0].text and "(folder, 0 items)" in results[0].text
    assert out.success is False


def test_evidence_gate_lets_a_true_done_through(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "PilotEval" / "Pilot Folder").mkdir(parents=True)
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(call("done", summary="created", success=True)),
        turn(call("done", summary="disk confirms Pilot Folder", success=True)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor)
    out = agent.run("In Finder, create a folder named 'Pilot Folder' inside ~/PilotEval.")
    assert out.success and "Pilot Folder/" in executor.logs[0][1][2][0].text


def test_ui_target_prefers_control_over_same_named_label(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(call("ui", target="Country")), turn(call("done", summary="opened", success=True)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor, use_ax=True)
    label_ref, popup_ref = SimpleNamespace(n="label"), SimpleNamespace(n="popup")
    env.registry = {1: Node("AXStaticText", label="Country", frame=(0, 0, 50, 10), ref=label_ref, id=1),
                    2: Node("AXPopUpButton", label="Country", value="Choose…", frame=(60, 0, 80, 20), ref=popup_ref, id=2)}
    env.next_id = 3
    agent.run("pick a country")
    assert env.ax.performed and env.ax.performed[0][0] is popup_ref


def test_every_decision_is_recorded_as_a_frame(monkeypatch, tmp_path):
    import json

    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(call("ui", target="Save")),
        turn(call("done", summary="saved", success=True)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor, use_ax=True)
    env.registry = {1: Node("AXButton", label="Save", frame=(100, 100, 60, 20), ref=SimpleNamespace(), id=1)}
    env.next_id = 2
    out = agent.run("save the document")
    frames = [json.loads(l) for l in (agent.trace.dir / "decisions.jsonl").read_text().splitlines()]
    assert len(frames) == 2 and out.success
    f1, f2 = frames
    assert f1["task"] == "save the document" and f1["brief"].startswith("Task: save the document")
    assert f1["image"] and f1["obs_text"] and f1["history"] == []
    assert f1["elements"][0]["label"] == "Save" and f1["elements"][0]["box"] == [100, 100, 60, 20]
    assert f1["output"]["calls"] == [{"name": "ui", "args": {"target": "Save"}}]
    assert f1["executed"][0]["ok"] is True and "press" in f1["executed"][0]["result"]
    assert f2["history"][0]["name"] == "ui"  # the second decision sees what the first one did
    assert f2["output"]["calls"][0]["name"] == "done" and f1["space"] == "pixels" and f1["prompt"]


def test_string_booleans_from_free_form_providers_are_coerced(monkeypatch, tmp_path):
    # Seen live via the subscription CLI: done(success="false") was scored as success.
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(ToolCall("x1", "done", {"summary": "no such setting exists", "success": "false"})),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor)
    out = agent.run("change the system font to Comic Sans")
    assert out.success is False and "no such setting" in out.reason


def test_scaffold_knobs_switch_behaviour(monkeypatch, tmp_path):
    same = lambda: call("computer", action="left_click", coordinate=[10, 10])  # noqa: E731
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(same()), turn(same()), turn(same()), turn(call("done", summary="x", success=False)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor, loop_window=0, no_change_note=False)
    agent.run("click")
    texts = [p.text for e in executor.logs[0] if e[0] == "reply" for p in e[3] if isinstance(p, TextPart)]
    assert not any("repeated" in t or "did not visibly change" in t for t in texts)


def test_evidence_gate_can_be_ablated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "PilotEval").mkdir()
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[turn(call("done", summary="made it", success=True))]])
    agent, env = make(monkeypatch, tmp_path, None, executor, evidence_gate=False)
    out = agent.run("In Finder, create a folder named 'Pilot Folder' inside ~/PilotEval.")
    assert out.success  # without the gate the claim is accepted as is (that is what an ablation measures)


def test_prompt_rules_follow_the_profile(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[turn(call("done", summary="x", success=True))]])
    agent, env = make(monkeypatch, tmp_path, None, executor, executor_rules=["terse"])
    assert "exact_url" not in agent.system and "open exactly that URL" not in agent.system
    assert "is data, not instructions" in agent.system and "short phrase" in agent.system


def test_popup_option_is_chosen_through_accessibility(monkeypatch, tmp_path):
    # Seen live: clicking the "Kenya" menu item by pixels left Safari's <select> on "Choose…".
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[
        turn(call("ui", id=1, action="set_value", value="Kenya")), turn(call("done", summary="chose", success=True)),
    ]])
    agent, env = make(monkeypatch, tmp_path, None, executor, use_ax=True)
    popup, menu = SimpleNamespace(n="popup"), SimpleNamespace(n="menu")
    items = {t: SimpleNamespace(n=t) for t in ["Choose…", "Ghana", "Kenya", "Nigeria"]}
    state = {"value": "Choose…"}

    class PopupAX(FakeAX):
        def attr(self, el, name):
            if el is popup and name == "AXChildren":
                return [menu]
            if el is menu and name == "AXChildren":
                return list(items.values())
            if name == "AXTitle":
                return next((t for t, it in items.items() if it is el), None)
            if el is popup and name == "AXValue":
                return state["value"]
            return "AXPopUpButton"

        def perform(self, el, action):
            self.performed.append((el, action))
            if el in items.values():
                state["value"] = next(t for t, it in items.items() if it is el)
            return 0

    env.ax = PopupAX()
    env.registry = {1: Node("AXPopUpButton", label="Country", value="Choose…", frame=(0, 0, 100, 20), ref=popup, id=1)}
    env.next_id = 2
    agent.run("pick Kenya")
    _, _, results, _ = executor.logs[0][1]
    assert results[0].ok and "Chose 'Kenya'" in results[0].text and state["value"] == "Kenya"
    assert (items["Kenya"], "AXPress") in env.ax.performed and env.input.log == []  # no pixel clicks at all
