"""Calibration with fake models: it must discover each model's real conventions."""

from __future__ import annotations

import re

from pilot.profile import Profile
from pilot.providers.base import Provider, Session
from pilot.replay import runner as rr
from pilot.tune import calibrate as cal
from pilot.tune import probes
from pilot.types import AssistantTurn, ToolCall


class FakeModel(Provider):
    """Clicks the requested button in its own coordinate convention; uses ids when it has them."""

    native_computer = False

    def __init__(self, spec, convention, uses_ids=True, budget=None):
        self.spec, self.convention, self.uses_ids, self.budget = spec, convention, uses_ids, budget

    def session(self, system, tools):
        model = self

        class S(Session):
            def send(self, parts):
                lead, listing = parts[0].text, parts[2].text
                m = re.search(r"click the '(.+?)' (?:button|row|field)", lead)
                if not m:  # a replay frame
                    return AssistantTurn(calls=[ToolCall("r", "done", {"summary": "x", "success": False})], stop="tool_use", latency=0.01)
                layout = next(lay for lay in probes.LAYOUTS if lay["target"] == m.group(1))
                if model.uses_ids and "[1] button" in listing:
                    i = list(layout["buttons"]).index(layout["target"]) + 1
                    return AssistantTurn(calls=[ToolCall("u", "ui", {"id": i})], stop="tool_use", latency=0.01)
                x0, y0, x1, y1 = layout["buttons"][layout["target"]]
                w = parts[1].width or probes.W
                cx, cy = (x0 + x1) / 2 * w / probes.W, (y0 + y1) / 2 * w / probes.W  # centre in the frame it was shown
                if model.budget and w > model.budget:  # silently shrunk: answers in the shrunken frame
                    f = model.budget / w
                    cx, cy = cx * f, cy * f
                if model.convention == "norm1000":
                    cx, cy = cx * 1000 / w, cy * 1000 / (w * probes.H / probes.W)
                return AssistantTurn(calls=[ToolCall("c", "computer", {"action": "left_click", "coordinate": [round(cx), round(cy)]})],
                                     stop="tool_use", latency=0.01)

            def reply(self, calls, results, extra=None):
                raise AssertionError

        return S()

    def complete_json(self, system, parts, schema):
        raise NotImplementedError


def frames(tmp_path):
    (tmp_path / "images").mkdir(exist_ok=True)
    (tmp_path / "images" / "a.png").write_bytes(b"png")
    hard = {"id": "h#1", "image": "images/a.png", "brief": "Task: x", "screen": [1470, 956], "space": "pixels", "elements": None,
            "executed": [], "label": {"kind": "hard", "tags": ["false-done"], "forbid": [{"tool": "done", "success": True}]}}
    soft = {"id": "s#1", "image": "images/a.png", "brief": "Task: y", "screen": [1470, 956], "space": "pixels", "elements": None,
            "executed": [{"name": "done", "args": {"summary": "s", "success": False}}], "label": {"kind": "soft"},
            "outcome": {"task_id": "t"}}
    return [hard, soft]


def run(tmp_path, monkeypatch, convention, uses_ids=True, budget=None):
    make = lambda spec: FakeModel(spec, convention, uses_ids, budget)  # noqa: E731
    for mod in (probes, rr, cal):
        monkeypatch.setattr(mod, "get_provider", make)
    cfg = cal.CalibrationConfig(efforts=("low", "medium"), frames=2, layouts=len(probes.LAYOUTS), parallel=1, sizes=(1280, 1024))
    return cal.calibrate("gemini:fake-flash", Profile("sub"), frames(tmp_path), tmp_path, cfg, tmp_path / "out", log=lambda m: None)


def test_discovers_normalised_coordinates(tmp_path, monkeypatch):
    prof, report = run(tmp_path, monkeypatch, "norm1000")
    assert prof.executor_coords == "norm1000" and prof.executor == "gemini:fake-flash" and prof.parent == "sub"
    assert "8/8 with norm1000" in prof.why["executor_coords"] and report.exists()


def test_discovers_pixel_coordinates(tmp_path, monkeypatch):
    prof, _ = run(tmp_path, monkeypatch, "pixels")
    assert prof.executor_coords == "pixels"


def test_keeps_element_list_when_ids_work_and_drops_it_when_dead_weight(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, "pixels", uses_ids=True)[0].use_ax is True
    prof, _ = run(tmp_path, monkeypatch, "pixels", uses_ids=False)  # ignores ids but clicks accurately
    assert prof.use_ax is False and "only cost tokens" in prof.why["use_ax"]


def test_effort_rule_takes_fastest_within_tolerance():
    sweep = {"low": {"score": 7, "graded": 8, "median_latency": 2.0}, "medium": {"score": 8, "graded": 8, "median_latency": 5.0},
             "high": {"score": 8, "graded": 8, "median_latency": 9.0}}
    assert cal.choose_effort(sweep, tolerance=1)[0] == "low"
    assert cal.choose_effort(sweep, tolerance=0)[0] == "medium"


def test_calibration_set_is_fixed_and_spans_failure_types():
    fs = [{"id": f"h{i}", "label": {"kind": "hard", "tags": ["safety", t]}} for i, t in enumerate(["injection", "injection", "false-done", "consent"])]
    fs += [{"id": f"s{i}", "label": {"kind": "soft"}, "outcome": {"task_id": f"t{i % 3}"}} for i in range(9)]
    a = cal.calibration_set(fs, 6)
    assert [f["id"] for f in a] == [f["id"] for f in cal.calibration_set(list(reversed(fs)), 6)]  # order-independent
    tags = [next(t for t in f["label"]["tags"] if t != "safety") for f in a if f["label"]["kind"] == "hard"]
    assert len(tags) == len(set(tags)) == 3
    assert len({f["outcome"]["task_id"] for f in a if f["label"]["kind"] == "soft"}) == 3


def test_detects_hidden_downscale_and_shrinks_screenshots(tmp_path, monkeypatch):
    # Seen live with Haiku 5.5 via the CLI: clicks scaled by ~0.93 on both axes at 1470 px.
    prof, report = run(tmp_path, monkeypatch, "pixels", uses_ids=False, budget=1370)
    assert prof.executor_coords == "pixels" and prof.max_long_edge < 1370
    assert "hidden resize" in prof.why["max_long_edge"]


def test_size_sweep_picks_smallest_screenshot_without_losing_accuracy(tmp_path, monkeypatch):
    # A perfect fake grounder loses nothing at 1024 px, so calibration should pick the cheapest size.
    prof, _ = run(tmp_path, monkeypatch, "pixels", uses_ids=True)
    assert prof.max_long_edge == 1024 and "smallest without losing accuracy" in prof.why["max_long_edge"]
