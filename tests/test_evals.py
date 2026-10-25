import urllib.request

from pilot.evals.fixtures import FixtureServer
from pilot.evals.runner import pareto, summarise
from pilot.evals.tasks import TASKS, Ctx


def row(preset, ok, s, cost, tags=("speed",), claimed=None):
    return {"preset": preset, "task": "t", "tags": list(tags), "pass": ok, "claimed": ok if claimed is None else claimed,
            "seconds": s, "model_seconds": s / 2, "cost": cost}


def test_summary_counts_false_done_and_safety():
    rows = [row("a", True, 10, 0.1), row("a", False, 20, 0.1, tags=("redteam",), claimed=True)]
    s = summarise(rows)
    assert s["passes"] == 1 and s["false_done"] == 1 and s["safety_fails"] == 1
    assert abs(s["cost_per_pass"] - 0.2) < 1e-9 and s["by_axis"]["redteam"] == (0, 1)


def test_pareto_front():
    sums = {
        "fast": summarise([row("fast", True, 10, 0.05), row("fast", False, 10, 0.05)]),      # 50%, 10 s, cheap
        "smart": summarise([row("smart", True, 30, 0.5), row("smart", True, 30, 0.5)]),      # 100%, slow, pricey
        "worse": summarise([row("worse", False, 40, 0.6), row("worse", True, 40, 0.6)]),     # dominated by smart
    }
    assert pareto(sums) == {"fast", "smart"}


def test_fixture_server_records_and_traps():
    s = FixtureServer(port=0).start()
    try:
        s.pages["/x"] = "<p>hi</p>"
        assert urllib.request.urlopen(s.url("/x")).read() == b"<p>hi</p>"
        urllib.request.urlopen(urllib.request.Request(s.url("/submit/f"), data=b"a=1&a=2&b=")).read()
        urllib.request.urlopen(s.url("/pwned?x=1")).read()
        assert s.last("f").data == {"a": ["1", "2"], "b": [""]} and s.trap_hits == ["/pwned?x=1"]
    finally:
        s.stop()


def test_every_task_is_well_formed():
    ids = [t.id for t in TASKS]
    assert len(ids) == len(set(ids)) >= 16
    axes = {"speed", "long", "intelligence", "precision", "redteam"}
    for t in TASKS:
        assert t.tags and set(t.tags) <= axes, t.id
    assert sum(1 for t in TASKS if "redteam" in t.tags) >= 4
    ctx = Ctx(FixtureServer(port=12345))
    assert "http://127.0.0.1:12345/apply.html" in next(t for t in TASKS if t.id == "apply-form").render(ctx)


def test_invalid_runs_are_excluded_from_scores():
    rows = [row("a", True, 10, 0.1), {**row("a", False, 1, 0.0), "invalid": True}]
    s = summarise(rows)
    assert s["n"] == 1 and s["pass_rate"] == 1.0


def test_infra_failures_are_recognised():
    from pilot.evals.runner import INFRA_FAILURE

    assert INFRA_FAILURE.match("InternalServerError: Error code: 503")
    assert INFRA_FAILURE.match("screen locked: the Mac locked during the run")
    assert not INFRA_FAILURE.match("stuck: the Export button is missing")


def test_evidence_pack_reads_named_paths(tmp_path, monkeypatch):
    from pilot.agent.runner import evidence

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "PilotEval" / "archive" / "2026").mkdir(parents=True)
    (tmp_path / "PilotEval" / "archive" / "2026" / "a.pdf").write_text("x")
    (tmp_path / "PilotEval" / ".DS_Store").write_text("junk")
    (tmp_path / "PilotEval" / "hottest.txt").write_text("Cairo 29 °C")
    ev = evidence("Move PDFs into ~/PilotEval/archive/2026 and write ~/PilotEval/hottest.txt; check ~/PilotEval/missing.txt")
    assert "a.pdf" in ev and "Cairo 29" in ev and "missing.txt: does not exist" in ev and ".DS_Store" not in ev


def test_usage_limit_is_infrastructure():
    from pilot.evals.runner import INFRA_FAILURE

    assert INFRA_FAILURE.match("Claude plan usage limit reached: You've hit your session limit · resets 5:50pm")
    assert INFRA_FAILURE.match("claude CLI error: You've hit your session limit")
