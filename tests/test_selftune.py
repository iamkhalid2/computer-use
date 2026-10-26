"""Self-tuning loop: sealed split, waste miner, typed proposer, pre-registered acceptance."""

from __future__ import annotations

import json
from types import SimpleNamespace

from pilot.profile import Profile
from pilot.providers.base import Provider
from pilot.tune import accept as acc
from pilot.tune import mine as mn
from pilot.tune import propose as pr
from pilot.types import AssistantTurn

TASKS = [SimpleNamespace(id=i, tags=tags) for i, tags in [
    ("a1", ["speed"]), ("a2", ["speed"]), ("a3", ["speed"]), ("b1", ["long"]), ("b2", ["long"]), ("b3", ["long"]),
    ("c1", ["precision"]), ("r1", ["redteam"]), ("r2", ["redteam", "precision"])]]


def test_split_is_stable_stratified_and_gates_redteam():
    sp = mn.split(TASKS)
    assert sp == mn.split(list(reversed(TASKS)))
    assert sp["r1"] == sp["r2"] == "gate"
    assert sum(sp[t] == "test" for t in ("a1", "a2", "a3")) == 1 and sum(sp[t] == "test" for t in ("b1", "b2", "b3")) == 1
    assert sp["c1"] == "dev"  # a lone task can't be both seen and sealed: it stays dev


def model(calls, t, tokens=5000, notes=("x",)):
    return {"kind": "model", "role": "executor", "t": t, "latency": 2.0, "in": tokens, "notes": list(notes), "calls": calls}


def shot():
    return [{"name": "computer", "args": {"action": "screenshot"}}]


def test_mine_run_classifies_waste():
    events = [
        {"kind": "start", "task": "x"},
        model([{"name": "shell", "args": {"command": "for f in *; do echo $f; done"}}], 1),
        {"kind": "action", "t": 1.1, "name": "shell", "args": {}, "ok": False, "result": "Not allowed without a human (`for` can change things). Find another way."},
        model(shot(), 2, 12000), model(shot(), 3, 24000),
        model([{"name": "open", "args": {"target": "x"}}], 4, 36000),
        {"kind": "action", "t": 4.1, "name": "open", "args": {}, "ok": False, "result": "Unable to find application named 'Foo 12'"},
        {"kind": "scaffold", "t": 5, "name": "await_editable", "waited": 0.6, "found": False},
        {"kind": "step_result", "t": 6, "outcome": "escalate", "reason": "no completion after 14 turns on this step"},
    ]
    clusters: dict = {}
    got = mn.mine_run(events, "r", "t", clusters)
    assert got["executor_calls"] == 4
    sigs = {k: c.count for k, c in clusters.items()}
    assert sigs["look-only"] == 2 and sigs["repeat"] == 1 and sigs["wait-timeout"] == 1
    assert "refused:Not allowed without a human (`for` can change things)" in sigs
    assert "error:open:Unable to find application named …" in sigs  # quotes and numbers normalised away
    assert "escalate:no completion after N turns on this step" in sigs
    assert clusters["context-growth"].tokens == (12000 - 5000) + (24000 - 5000) + (36000 - 5000)


def write_run(root, name, task_text, events):
    d = root / "runs" / name
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in [{"kind": "start", "task": task_text}] + events))


def test_mine_never_reads_sealed_tasks(tmp_path):
    sp = mn.split(TASKS)
    sealed = next(t for t in ("a1", "a2", "a3") if sp[t] == "test")
    seen = next(t for t in ("a1", "a2", "a3") if sp[t] == "dev")
    waste = [model(shot(), 1)]
    write_run(tmp_path, "20261026-100000-s", f"do {sealed} at http://127.0.0.1:5000/x", waste)
    write_run(tmp_path, "20261026-100001-d", f"do {seen} at http://127.0.0.1:5000/x", waste)
    write_run(tmp_path, "20261025-090000-old", f"do {sealed} at http://127.0.0.1:4000/x", waste)  # no bench row: matched by text
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "pilotbench-1.json").write_text(json.dumps([
        {"task": sealed, "report": "runs/20261026-100000-s/report.html", "invalid": False},
        {"task": seen, "report": "runs/20261026-100001-d/report.html", "invalid": False}]))
    m = mn.mine(tmp_path / "runs", tmp_path / "evals", TASKS, "dev", rounds=None)
    assert m["runs"] == 1 and m["clusters"][0]["tasks"] == [seen]
    assert mn.mine(tmp_path / "runs", tmp_path / "evals", TASKS, "test", rounds=None)["runs"] == 2


def test_validate_keeps_only_typed_real_changes():
    prof = Profile("sub")
    raw = {"proposals": [
        {"knob": "max_long_edge", "value_json": "1024"},
        {"knob": "max_long_edge", "value_json": "99999"},  # out of range
        {"knob": "executor", "value_json": "\"claude-code:claude-haiku-5-5\""},  # models are calibration's job
        {"knob": "approve", "value_json": "\"none\""},  # an invariant, not a knob
        {"knob": "use_ax", "value_json": "true"},  # no change
    ]}
    ok, dropped = pr.validate(raw, prof)
    assert [(p["knob"], p["value"]) for p in ok] == [("max_long_edge", 1024)] and len(dropped) == 4


class Proposer(Provider):
    native_computer = False

    def __init__(self, spec):
        self.spec = spec

    def session(self, system, tools):
        raise AssertionError

    def complete_json(self, system, parts, schema):
        assert "Ledger of past experiments" in parts[0].text and "sub-lean" in parts[0].text
        return ({"proposals": [{"knob": "max_long_edge", "value_json": "1280", "clusters": ["context-growth"], "mechanism": "smaller images",
                                "expected": "fewer tokens", "risk": "grounding"}],
                 "harness_notes": [{"cluster": "error:open", "evidence": "e", "suggestion": "s"}]}, AssistantTurn(latency=1.0))


def test_propose_writes_candidates_and_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "get_provider", Proposer)
    led = tmp_path / "ledger.jsonl"
    pr.record({"candidate": "sub-lean", "base": "sub", "knob": "executor_rules", "value": [], "status": "rejected", "evidence": "x"}, led)
    mined = {"runs": 3, "executor_calls": 10, "executor_in": 100000, "clusters": [{"signature": "context-growth"}]}
    cands, rep, path = pr.propose(Profile("sub"), mined, tmp_path, ledger_path=led)
    assert len(cands) == 1 and cands[0].max_long_edge == 1280 and "unconfirmed" in cands[0].why["max_long_edge"]
    assert pr.latest(pr.ledger(led))[cands[0].name]["status"] == "proposed" and rep["raw"]["harness_notes"]


def row(preset, task, ok=True, calls=10, actions=10, invalid=False, tokens=100000):
    return {"preset": preset, "task": task, "pass": ok, "model_calls": calls, "actions": actions, "seconds": 10, "cost": 0.1,
            "invalid": invalid, "tokens_in": tokens}


def test_decision_rule():
    gate = {"r1"}
    base = [row("b", "t1"), row("b", "r1")]
    assert acc.decide(base + [row("c", "t1", calls=8), row("c", "r1", calls=8)], "c", "b", gate)["status"] == "accepted"
    assert acc.decide(base + [row("c", "t1", calls=10), row("c", "r1", calls=9.5)], "c", "b", gate)["status"] == "rejected"  # < 5%
    assert acc.decide(base + [row("c", "t1", calls=5), row("c", "r1", ok=False)], "c", "b", gate)["why"].startswith("red-team")
    assert acc.decide(base + [row("c", "t1", calls=5, actions=30), row("c", "r1")], "c", "b", gate)["status"] == "rejected"
    assert acc.decide(base + [row("c", "t1", invalid=True), row("c", "r1")], "c", "b", gate)["status"] == "inconclusive"
    # Fewer tokens count only if calls and actions hold (within noise): a cheaper call that needs more calls is no gain.
    assert acc.decide(base + [row("c", "t1", tokens=60000, calls=11), row("c", "r1", tokens=60000)], "c", "b", gate)["status"] == "accepted"
    assert acc.decide(base + [row("c", "t1", tokens=60000, calls=13), row("c", "r1", tokens=60000)], "c", "b", gate)["status"] == "rejected"
    assert acc.decide(base + [row("c", "t1", tokens=90000), row("c", "r1", tokens=90000)], "c", "b", gate)["status"] == "rejected"


def test_accept_adopts_into_base_and_records(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    base = Profile("mybase")
    base.save(tmp_path / "profiles" / "mybase.json")
    cand = base.mutate("mybase-x", why="smaller images [proposed; unconfirmed until pilot tune accept]", max_long_edge=1280)
    cpath = cand.save(tmp_path / "profiles" / "mybase-x.json")
    led = tmp_path / "ledger.jsonl"

    def fake_run(presets, ids, repeat, overrides, out_dir):
        rows = [row(str(cpath), t, calls=7) for t in ids] + [row("mybase", t) for t in ids]
        p = tmp_path / "bench.json"
        p.write_text(json.dumps(rows))
        return p

    v = acc.accept(str(cpath), "mybase", tmp_path, adopt=True, run=fake_run, ledger_path=led)
    assert v["status"] == "accepted" and v["adopted"]
    adopted = Profile.load("mybase")
    assert adopted.max_long_edge == 1280 and "accepted bench.json" in adopted.why["max_long_edge"]
    assert "unconfirmed" not in adopted.why["max_long_edge"] and adopted.why["max_long_edge"].startswith("smaller images")
    statuses = [e["status"] for e in pr.ledger(led)]
    assert statuses == ["testing", "accepted"]  # the rule was on record before the run


def test_inert_knobs_are_hidden_from_the_proposer():
    rows = {r["knob"] for r in pr.knob_table(Profile("sub"))}  # claude-code executor: the CLI keeps its own context
    assert "keep_images" not in rows and "max_long_edge" in rows
    assert "keep_images" in {r["knob"] for r in pr.knob_table(Profile("x", {"executor": "openai:gpt-x"}))}


def test_settings_deep_links_open_as_urls_but_calls_do_not(monkeypatch):
    from pilot.mac import apps

    seen = []
    monkeypatch.setattr(apps, "run", lambda cmd, timeout=20: seen.append(cmd) or apps.RunResult(True, "", 0))
    apps.open_target("x-apple.systempreferences:com.apple.Appearance-Settings.extension")
    apps.open_target("facetime:+15550100")
    assert seen[0] == ["open", "x-apple.systempreferences:com.apple.Appearance-Settings.extension"]
    assert seen[1] == ["open", "-a", "facetime:+15550100"]  # not a URL to us: fails as an app name, places no call
