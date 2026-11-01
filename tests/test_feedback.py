"""Thumbs up or down on real runs, and what each one feeds: replay items and tune mine."""

from __future__ import annotations

import json

import pytest

from pilot import feedback
from pilot.replay import frames as fr
from pilot.replay import seed
from pilot.replay.grader import grade
from pilot.tune import mine as mn


def real_run(runs, name, claimed=True, calls=None):
    run = runs / name
    run.mkdir(parents=True)
    events = [{"kind": "start", "task": "tidy my desktop"},
              {"kind": "model", "role": "executor", "calls": [{"name": "done", "args": {"summary": "tidied", "success": True}}],
               "in": 4000, "t": 9.0}]
    (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (run / "summary.json").write_text(json.dumps({"success": claimed}))
    (run / "001-observe.png").write_bytes(b"png")
    calls = calls or [{"name": "done", "args": {"summary": "tidied", "success": "true"}}]
    decision = {"id": f"{name}#1", "kind": "decision", "task": "tidy my desktop", "brief": "Task: tidy my desktop", "history": [],
                "notes": [], "image": "001-observe.png", "obs_text": "Frontmost app: Finder", "elements": [], "space": "pixels",
                "screen": [1470, 956], "output": {"calls": calls}, "executed": [{**c, "ok": True, "result": "ok"} for c in calls]}
    (run / "decisions.jsonl").write_text(json.dumps(decision) + "\n")
    return run


def test_record_read_and_the_prompt(tmp_path):
    run = real_run(tmp_path / "runs", "20261028-100000-tidy")
    answers = iter(["n", "it moved my files into Trash"])
    fb = feedback.ask(run, True, lambda _: next(answers))
    assert fb["rating"] == "down" and fb["note"] == "it moved my files into Trash" and feedback.false_done(fb)
    assert feedback.read(run) == fb
    assert feedback.ask(run, True, lambda _: "") is None  # Enter skips, and the old rating stays
    assert feedback.read(run)["rating"] == "down"

    def eof(_):
        raise EOFError

    assert feedback.ask(run, True, eof) is None
    assert feedback.latest_run(tmp_path / "runs") == run
    with pytest.raises(ValueError):
        feedback.record(run, "meh")


def test_rated_runs_become_replay_items(tmp_path):
    runs = tmp_path / "runs"
    real_run(runs, "20261028-100000-good")
    feedback.record(runs / "20261028-100000-good", "up")
    real_run(runs, "20261028-110000-bad")
    feedback.record(runs / "20261028-110000-bad", "down", "the files went to Trash")
    real_run(runs, "20261028-120000-unrated")
    out = tmp_path / "replay"
    fr.build(runs, tmp_path / "evals", out)
    seed.write(out, fr.load(out))
    fr.build(runs, tmp_path / "evals", out)
    by = {f["id"].split("#")[0].rsplit("-", 1)[1]: f for f in fr.load(out)}
    assert by["good"]["label"]["kind"] == "soft" and by["good"]["outcome"]["source"] == "feedback"
    assert by["unrated"]["label"]["kind"] == "unlabeled"
    bad = by["bad"]
    assert bad["label"]["kind"] == "hard" and bad["label"]["tags"] == ["false-done", "feedback"]
    assert "the files went to Trash" in bad["label"]["note"]
    # the recorded claim now fails; checking the result first passes
    assert not grade(bad, [{"name": "done", "args": {"summary": "tidied", "success": True}}])["pass"]
    assert grade(bad, [{"name": "computer", "args": {"action": "screenshot"}}])["pass"]


def test_down_without_a_success_claim_is_not_a_false_done(tmp_path):
    runs = tmp_path / "runs"
    real_run(runs, "20261028-130000-gaveup", claimed=False,
             calls=[{"name": "done", "args": {"summary": "couldn't", "success": False}}])
    fb = feedback.record(runs / "20261028-130000-gaveup", "down")
    assert not feedback.false_done(fb)
    out = tmp_path / "replay"
    fr.build(runs, tmp_path / "evals", out)
    assert seed.labels(fr.load(out)) == []


def test_mine_counts_a_rated_down_run_as_waste(tmp_path):
    runs = tmp_path / "runs"
    real_run(runs, "20261028-140000-bad")
    feedback.record(runs / "20261028-140000-bad", "down", "wrong folder")
    (tmp_path / "evals").mkdir()
    m = mn.mine(runs, tmp_path / "evals", [], "dev", rounds=None)
    top = {c["signature"]: c for c in m["clusters"]}["you: claimed done, wasn't"]
    assert top["runs"] == 1 and top["examples"][0]["note"] == "wrong folder" and top["turns_lost"] == 1
