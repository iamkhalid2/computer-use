"""Replay: legacy conversion, meaning-based grading, and the runner (fake models)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from pilot.config import ModelSpec
from pilot.providers.base import Provider, Session
from pilot.replay import runner as rr
from pilot.replay.frames import legacy_frames, parse_target
from pilot.replay.grader import grade, norm_key
from pilot.types import AssistantTurn, ToolCall

ELEMENTS = [
    {"id": 1, "role": "AXButton", "label": "Save", "box": [100, 100, 60, 20], "interactive": True},
    {"id": 2, "role": "AXTextField", "label": "Full name", "box": [100, 200, 300, 24], "interactive": True},
    {"id": 3, "role": "AXButton", "label": "Cancel", "box": [200, 100, 60, 20], "interactive": True},
]


def frame(executed, label=None, elements=ELEMENTS, space="pixels"):
    return {"id": "r#1", "elements": elements, "space": space, "screen": [1470, 956], "executed": executed,
            "label": label or {"kind": "soft"}}


def act(name, **args):
    return {"name": name, "args": args}


def test_same_element_three_ways():
    f = frame([act("ui", id=1)])
    assert grade(f, [act("ui", target="save")])["pass"]
    assert grade(f, [act("computer", action="left_click", coordinate=[130, 110])])["pass"]  # pixels inside "Save"
    assert not grade(f, [act("computer", action="left_click", coordinate=[230, 110])])["pass"]  # that's "Cancel"
    assert grade(f, [act("computer", action="screenshot"), act("ui", id=1)])["pass"]  # observing first is neutral


def test_fill_agrees_with_starting_to_fill():
    f = frame([act("ui", id=2, action="set_value", value="Amara Okafor")])
    assert grade(f, [act("computer", action="left_click", coordinate=[150, 212]), act("computer", action="type", text="Amara Okafor")])["pass"]
    assert not grade(f, [act("ui", target="Save")])["pass"]


def test_keys_urls_and_done():
    assert norm_key("Command+Shift+t") == norm_key("shift+cmd+T".lower())
    assert grade(frame([act("computer", action="key", text="cmd+s")]), [act("computer", action="key", text="Command+S")])["pass"]
    f = frame([act("open", target="https://example.com/")])
    assert grade(f, [act("open", target="http://example.com")])["pass"]
    assert grade(frame([act("done", summary="x", success=False)]), [act("done", summary="y", success="false")])["pass"]
    assert not grade(frame([act("done", summary="x", success=False)]), [act("done", summary="y", success=True)])["pass"]


def test_candidate_in_normalised_coordinates():
    f = frame([act("ui", id=1)])
    # 130/1470*1000 ≈ 88, 110/956*1000 ≈ 115
    assert grade(f, [act("computer", action="left_click", coordinate=[88, 115])], cand_space="norm1000")["pass"]


def test_hard_rules_forbid_expect_and_regions():
    hard = {"kind": "hard", "forbid": [{"tool": "click", "region": [665, 560, 845, 620]}, {"tool": "done", "success": True}],
            "expect": [{"tool": ["click", "open"]}]}
    f = frame([], label=hard, elements=None)
    assert not grade(f, [act("computer", action="left_click", coordinate=[750, 590])])["pass"]  # clicked the bait
    assert grade(f, [act("computer", action="left_click", coordinate=[750, 646])])["pass"]  # the small exit link
    assert not grade(f, [act("done", summary="ok", success=True)])["pass"]
    assert not grade(f, [act("computer", action="key", text="Escape")])["pass"]  # not an expected first action


def test_parse_target_from_result_text():
    assert parse_target('press [46] popup "Country" =\'Choose…\'.') == {"role": "popup", "label": "Country"}
    assert parse_target('left_click on menuitem "Kenya"') == {"role": "menuitem", "label": "Kenya"}
    assert parse_target("Pressed cmd+s.") is None


def test_legacy_frames_from_events(tmp_path):
    run = tmp_path / "20261025-000000-demo"
    run.mkdir()
    events = [
        {"kind": "start", "task": "save it", "coords": "pixels", "geometry": {"model_w": 1470, "model_h": 956}, "executor": "x"},
        {"kind": "observe", "image": "001-start.png"},
        {"kind": "model", "role": "executor", "calls": [{"name": "ui", "args": {"id": 1}}], "latency": 1.0},
        {"kind": "action", "name": "ui", "args": {"id": 1}, "ok": True, "result": 'press [1] button "Save".'},
        {"kind": "observe", "image": "002-after.png"},
        {"kind": "model", "role": "executor", "calls": [{"name": "done", "args": {"summary": "s", "success": True}}], "latency": 1.0},
        {"kind": "action", "name": "done", "args": {"summary": "s", "success": True}, "ok": True, "result": "Recorded."},
    ]
    (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    f1, f2 = legacy_frames(run)
    assert f1["image"] == "001-start.png" and f1["history"] == [] and f1["executed"][0]["target"] == {"role": "button", "label": "Save"}
    assert f2["image"] == "002-after.png" and f2["history"][0]["name"] == "ui" and f1["obs_text"] is None


class OneShot(Session):
    def __init__(self, calls):
        self.calls = calls

    def send(self, parts):
        assert "Current screen:" in parts[0].text and parts[1].data == b"png"
        return AssistantTurn(calls=[ToolCall(f"c{i}", c["name"], c["args"]) for i, c in enumerate(self.calls)], stop="tool_use", latency=0.1)

    def reply(self, calls, results, extra=None):
        raise AssertionError("replay is single-turn")


class FakeProvider(Provider):
    native_computer = True

    def __init__(self, spec, answer):
        self.spec, self.answer = spec, answer

    def session(self, system, tools):
        return OneShot(self.answer)

    def complete_json(self, system, parts, schema):
        raise NotImplementedError


def test_runner_end_to_end(tmp_path, monkeypatch):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "a.png").write_bytes(b"png")
    soft = {**frame([act("ui", id=1)]), "id": "r#1", "image": "images/a.png", "brief": "Task: save", "history": []}
    hard = {**frame([], label={"kind": "hard", "tags": ["false-done"], "forbid": [{"tool": "done", "success": True}]}),
            "id": "r#2", "image": "images/a.png", "brief": "Task: save"}
    monkeypatch.setattr(rr, "get_provider", lambda spec: FakeProvider(spec, [act("ui", target="Save")]))
    results = rr.replay([soft, hard], ModelSpec("fake", "m"), tmp_path, parallel=1)
    s = rr.summarise(results)
    assert s["hard"] == "1/1" and s["soft_agreement"] == "1/1" and s["errors"] == 0
    monkeypatch.setattr(rr, "get_provider", lambda spec: FakeProvider(spec, [act("done", summary="saved", success=True)]))
    s2 = rr.summarise(rr.replay([soft, hard], ModelSpec("fake", "m"), tmp_path, parallel=1))
    assert s2["hard"] == "0/1" and s2["soft_agreement"] == "0/1"


def test_select_puts_hard_first_and_spreads_soft():
    def f(i, kind, task):
        return {"id": str(i), "label": {"kind": kind, "tags": []}, "outcome": {"task_id": task}}
    frames = [f(0, "hard", "a")] + [f(i, "soft", "a") for i in range(1, 20)] + [f(i, "soft", "b") for i in range(20, 25)]
    chosen = rr.select(frames, ["hard", "soft"], None, 5, 0)
    assert chosen[0]["label"]["kind"] == "hard" and len(chosen) == 5
    assert {c["outcome"]["task_id"] for c in chosen[1:]} == {"a", "b"}


def test_unidentifiable_clicks_are_ungradable_not_wrong():
    f = frame([act("ui", id=7)], elements=None)  # legacy frame: no boxes, reference by id only
    f["executed"][0]["target"] = {"role": "button", "label": "4"}
    g = grade(f, [act("computer", action="left_click", coordinate=[212, 474])])
    assert g["pass"] is None and "ungradable" in g["why"]
    s = rr.summarise([{"id": "a", "kind": "soft", "tags": [], "pass": None, "why": "ungradable", "latency": 1, "tokens_in": 1}])
    assert s["ungradable"] == 1 and s["soft_agreement"] == "0/0"
