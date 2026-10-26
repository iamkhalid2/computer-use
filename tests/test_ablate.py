"""Ablation with a fake model whose behaviour depends on one prompt rule; trace audit; list variants."""

from __future__ import annotations

import json

from pilot.profile import Profile
from pilot.providers.base import Provider, Session
from pilot.replay import runner as rr
from pilot.tune import ablate as ab
from pilot.types import AssistantTurn, ToolCall


class RuleBound(Provider):
    """Fakes success unless the prompt carries the `impossible` rule; flails when it can't see."""

    native_computer = False
    sees = True  # False: ignores the screen entirely (a test set has no power against it)

    def __init__(self, spec):
        self.spec = spec

    def session(self, system, tools):
        honest = "never fake success" in system
        sees = self.sees

        class S(Session):
            def send(self, parts):
                if sees and "No element list" in parts[2].text:
                    return AssistantTurn(calls=[ToolCall("c", "computer", {"action": "left_click", "coordinate": [5, 5]})], stop="tool_use", latency=0.01)
                if honest:
                    return AssistantTurn(calls=[ToolCall("u", "ui", {"target": "Save"})], stop="tool_use", latency=0.01)
                return AssistantTurn(calls=[ToolCall("d", "done", {"summary": "x", "success": True})], stop="tool_use", latency=0.01)

            def reply(self, calls, results, extra=None):
                raise AssertionError

        return S()

    def complete_json(self, system, parts, schema):
        raise NotImplementedError


def frames(tmp_path, tags):
    (tmp_path / "images").mkdir(exist_ok=True)
    (tmp_path / "images" / "a.png").write_bytes(b"png")
    base = {"image": "images/a.png", "screen": [1470, 956], "space": "pixels", "elements": None, "source": "recorded"}
    hard = {**base, "id": "h#1", "brief": "Task: x", "executed": [],
            "label": {"kind": "hard", "tags": tags, "forbid": [{"tool": "done", "success": True}]}}
    soft = {**base, "id": "s#1", "brief": "Task: y", "executed": [{"name": "ui", "args": {"target": "Save"}}],
            "label": {"kind": "soft"}, "outcome": {"task_id": "t"}}
    return [hard, soft]


def run(tmp_path, monkeypatch, tags, budget=200, sees=True):
    monkeypatch.setattr(RuleBound, "sees", sees)
    monkeypatch.setattr(rr, "get_provider", RuleBound)
    monkeypatch.chdir(tmp_path)
    cfg = ab.AblationConfig(frames=2, budget=budget, tolerance=0, parallel=1, outline_lines=())
    return ab.ablate(Profile("sub"), frames(tmp_path, tags), tmp_path, cfg, tmp_path / "out", log=lambda m: None)


def test_bisection_keeps_only_the_rule_that_matters(tmp_path, monkeypatch):
    cand, report = run(tmp_path, monkeypatch, ["false-done", "impossible"])
    # Everything else goes, except rules that act across turns and rules guarding a failure the set can't exercise.
    assert cand.executor_rules == ["action_priority", "batching", "check_after", "exact_url", "impossible"]
    assert cand.parent == "sub" and "confirm with pilot eval" in cand.why["executor_rules"]
    data = json.loads(report.read_text())
    rule_tests = [t for t in data["tests"] if t["label"].startswith("remove")]
    assert len(rule_tests) <= 5  # never worse than one test per candidate rule (5 here), better when most are dead weight


def test_guard_rules_are_kept_when_their_failure_is_not_in_the_set(tmp_path, monkeypatch):
    cand, report = run(tmp_path, monkeypatch, ["consent"])
    # Without a hard item of "impossible", replay cannot tell the rule matters: it must not be dropped.
    assert "impossible" in cand.executor_rules and "done" in cand.executor_rules
    assert "impossible" in json.loads(report.read_text())["decisions"]["untested_rules"]


def test_budget_stops_the_search(tmp_path, monkeypatch):
    cand, report = run(tmp_path, monkeypatch, ["impossible"], budget=4)
    assert json.loads(report.read_text())["calls_used"] <= 4
    assert cand.executor_rules == Profile("sub").executor_rules  # nothing proven, nothing dropped


def test_no_power_means_no_proposal(tmp_path, monkeypatch):
    cand, report = run(tmp_path, monkeypatch, ["false-done", "impossible"], sees=False)
    data = json.loads(report.read_text())
    assert data["decisions"]["powered"] is False and not Profile("sub").diff(cand)


def test_flaky_items_are_set_aside():
    a = [{"id": "x", "pass": True}, {"id": "y", "pass": True}, {"id": "z", "error": "boom"}]
    b = [{"id": "x", "pass": False}, {"id": "y", "pass": True}, {"id": "z", "pass": True}]
    assert ab.flaky(a, b) == {"x"}
    assert ab.score([{"id": "x", "kind": "hard", "pass": True, "tokens_in": 1, "latency": 1}], {"x"})["graded"] == 0


def test_render_observation_variants():
    elements = [{"id": i, "role": "AXButton", "label": f"B{i}", "box": [10, 10 + 20 * i, 50, 18], "enabled": True} for i in range(1, 60)]
    listing = "\n".join(f'[{e["id"]}] button "{e["label"]}"' for e in elements)
    frame = {"obs_text": "Frontmost app: Finder\nUI elements (ids):\n" + listing, "elements": elements}
    assert "No element list" in rr.render_observation(frame, {"use_ax": False})
    assert rr.render_observation(frame, None) == frame["obs_text"]
    short = rr.render_observation(frame, {"outline_max_lines": 10})
    assert short.startswith("Frontmost app: Finder") and len(short) < len(frame["obs_text"])


def test_audit_reads_course_changes(tmp_path):
    def model(calls):
        return {"kind": "model", "role": "executor", "calls": calls}

    click = [{"name": "computer", "args": {"action": "left_click"}}]
    key = [{"name": "computer", "args": {"action": "key"}}]
    done = [{"name": "done", "args": {"success": True}}]
    events = []
    for _ in range(3):
        events += [model(click), {"kind": "scaffold", "name": "loop_note"}, model(key)]  # changed course each time
        events += [model(done), {"kind": "evidence_gate"}, model(done)]  # confirmed each time
    (tmp_path / "r1").mkdir()
    (tmp_path / "r1" / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    out = ab.audit(tmp_path)
    assert out["loop_note"]["changed"] == 3 and out["loop_note"]["verdict"].startswith("keep")
    assert out["evidence_gate"]["confirmed"] == 3 and "catch rate < 100%" in out["evidence_gate"]["verdict"]
