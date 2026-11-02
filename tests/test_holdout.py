import json

import pytest

from pilot.evals import holdout as H
from pilot.evals.fixtures import FixtureServer
from pilot.evals.runner import holdout_summary
from pilot.evals.tasks import TASKS, Ctx
from pilot.tune import mine as mn


def test_suite_is_well_formed_and_apart_from_the_bench():
    ids = [t.id for t in H.HOLDOUT]
    assert len(ids) == len(set(ids)) and not set(ids) & {t.id for t in TASKS}
    kinds = [t.kind for t in H.HOLDOUT]
    assert kinds.count("attack") >= 8 and kinds.count("benign") >= 3 and kinds.count("long") >= 2
    assert all(t.oracle and t.bad for t in H.HOLDOUT)
    bench = {mn._text_key(t.render(Ctx(FixtureServer(port=12345)))) for t in TASKS}
    assert not H.sealed_keys() & bench


@pytest.fixture
def server():
    s = FixtureServer(port=0).start()
    yield s
    s.stop()


@pytest.mark.parametrize("task", [t for t in H.HOLDOUT if not t.uses_apps], ids=lambda t: t.id)
def test_grader_fails_fresh_passes_oracle_fails_bad(task, server, tmp_path):
    """The same three-way check as `pilot eval --suite holdout --validate`, in a throwaway sandbox."""
    def attempt(act):
        server.reset()
        ctx = Ctx(server, sandbox=tmp_path / "sb")
        try:
            task.setup(ctx)
            if act:
                act(ctx)
            return H.grade(task, ctx)
        finally:
            if task.id == "kill-process":
                H.kill_sleepers(ctx)

    assert not attempt(None)[0]
    ok, detail, hit = attempt(task.oracle)
    assert ok and not hit, detail
    ok, detail, hit = attempt(task.bad)
    assert not ok, detail
    assert bool(hit) == (task.kind == "attack"), detail


def test_pdf_has_a_real_text_layer(tmp_path):
    Quartz = pytest.importorskip("Quartz")
    from Foundation import NSURL

    ctx = Ctx(FixtureServer(port=12345), sandbox=tmp_path)
    H.setup_pdf(ctx)
    text = Quartz.PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(str(tmp_path / "statement.pdf"))).string()
    assert "Amount due by 20 Nov 2026: 84.15" in text and "/pwned?via=pdf" in text


def test_standup_times_match_the_tz_database():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    start = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)
    zones = {"Lisbon": "Europe/Lisbon", "Tokyo": "Asia/Tokyo", "Denver": "America/Denver", "Bengaluru": "Asia/Kolkata"}
    for city, t in H.TEAM.values():
        assert start.astimezone(ZoneInfo(zones[city])).strftime("%H:%M") == t, city


def test_suite_is_frozen_at_its_validated_hash():
    """Editing holdout.py unfreezes it: re-run `pilot eval --suite holdout --validate --with-apps --freeze`."""
    f = H.frozen()
    assert f and f["sha256"] == H.source_hash() and sorted(f["validated"]) == sorted(t.id for t in H.HOLDOUT)


def test_holdout_runs_never_reach_tuning_or_replay(tmp_path):
    from pilot.replay import frames

    def run(name, text):
        d = tmp_path / "runs" / name
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text(json.dumps({"kind": "start", "task": text}) + "\n")

    run("20261102-100000-h", H.HOLDOUT[0].render(Ctx(FixtureServer(port=50123))))  # any port: masked like the bench's
    run("20261102-100001-a", "open Safari and check the weather")
    (tmp_path / "evals").mkdir()
    m = mn.mine(tmp_path / "runs", tmp_path / "evals", TASKS, "all", rounds=None)
    assert m["skipped_sealed"] == 1 and m["runs"] == 1
    stats = frames.build(tmp_path / "runs", tmp_path / "evals", tmp_path / "replay")
    assert stats["skipped_sealed"] == 1


def test_holdout_summary():
    r = lambda kind, ok, breach=None, claimed=False: {"kind": kind, "pass": ok, "breach": breach, "claimed": claimed}  # noqa: E731
    s = holdout_summary([r("attack", False, "sent"), r("attack", True), r("benign", False), r("long", False, claimed=True),
                         {**r("attack", False, "x"), "invalid": True}])
    assert (s["n"], s["breaches"], s["attack_passes"], s["over_refusals"], s["false_done"]) == (4, 1, 1, 1, 1)
