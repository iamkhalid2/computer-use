"""Playbooks end to end: the planner leads a new kind of job, the executor leads the next one from what was
distilled, and the belief in a playbook decides who leads after that."""

from __future__ import annotations

import json

from pilot.agent import runner as runner_mod
from pilot.agent.runner import Agent
from pilot.config import ModelSpec, Settings
from pilot.console import Reporter
from pilot.skills import Skills, curve
from test_agent import FakeEnv, FakeProvider, call, turn

PLAN = {"strategy": "search then queue", "needs_answer": False, "impossible": False, "steps": [
    {"goal": "search the song and play it", "done_when": "playing", "hint": ""},
    {"goal": "queue the other song", "done_when": "queued", "hint": ""}]}
DONE = {"complete": True, "answer": "", "missing": "", "steps": []}
BOOK = {"playbooks": [{"covers": "whole", "when": "Play a named song in Spotify and queue another",
                       "apps": ["Spotify"], "keywords": ["play", "song", "queue", "music", "spotify"],
                       "route": ["Open Spotify and click the search box at the top", "Type <song>, wait a second, double-click the top result",
                                 "The app shows no readable text: never wait for text, read the screenshot",
                                 "Search <other song>, right-click the top result, choose Add to queue"]}]}
FIRST = "play bangarang by skrillex on spotify and add mumbai power to the queue"
SECOND = "play get lucky on spotify and queue instant crush after it"


def agent_for(monkeypatch, tmp_path, planner, executor, store):
    providers = {"planner-model": planner, "executor-model": executor}
    monkeypatch.setattr(runner_mod, "get_provider", lambda spec, keep_images=3: providers[spec.model])
    monkeypatch.setattr(runner_mod.apps, "screen_locked", lambda: False)
    s = Settings(planner=ModelSpec("fake", "planner-model"), executor=ModelSpec("fake", "executor-model"), interactive=False,
                 approve="none", runs_dir=tmp_path / "runs", playbooks="learn")
    return Agent(s, FakeEnv(), Reporter(quiet=True), skills=store)


def done(summary="ok"):
    return [turn(call("done", summary=summary, success=True))]


def test_first_run_is_led_by_the_planner_and_the_second_by_the_executor(monkeypatch, tmp_path):
    store = Skills(tmp_path / "exp")
    executor = FakeProvider(ModelSpec("fake", "executor-model"), json_script=[BOOK], sessions=[done("playing"), done("queued"), done("both")])
    planner = FakeProvider(ModelSpec("fake", "planner-model"), json_script=[PLAN, DONE])
    agent = agent_for(monkeypatch, tmp_path, planner, executor, store)

    # 1. A new kind of job: the planner plans it and checks it, then the executor model distils the route.
    out = agent.run(FIRST)
    assert out.success and len(planner.json_calls) == 2 and agent.led is None
    assert len(store.books) == 1
    pb = next(iter(store.books.values()))
    assert pb.alpha == 1.5 and pb.beta == 1.0 and pb.cold["planner"] == 2  # its first evidence is the success it came from
    assert "bangarang" not in json.dumps(BOOK).lower()  # (the fixture, like a real playbook, carries no task data)
    assert Skills(tmp_path / "exp").books[pb.id].route == pb.route  # saved to disk

    # 2. The same kind of job, new songs: the executor leads from the playbook and the planner is never called.
    out = agent.run(SECOND)
    assert out.success and agent.led is not None and agent.led.id == pb.id
    assert len(planner.json_calls) == 2  # unchanged: no plan, no verify
    brief = executor.logs[2][0][1][0].text
    assert "You have done this kind of job before" in brief and "right-click the top result" in brief and "Do the whole task" in brief
    assert pb.led == 1 and pb.alpha == 2.0 and pb.warm["planner"] == 0 and pb.warm["calls"] < pb.cold["calls"]
    kinds = [json.loads(x)["kind"] for x in (tmp_path / "runs").glob("*get-lucky*").__next__().joinpath("events.jsonl").read_text().splitlines()]
    assert "lead" in kinds and "playbook" in kinds and "plan" not in kinds


def test_a_playbook_that_keeps_failing_hands_the_job_back_to_the_planner(monkeypatch, tmp_path):
    store = Skills(tmp_path / "exp")
    pb = store.add(BOOK["playbooks"][0], "seed")
    def fail():
        return [turn(call("done", summary="could not", success=False))]

    executor = FakeProvider(ModelSpec("fake", "executor-model"), json_script=[BOOK], sessions=[fail(), fail(), done(), done()])
    planner = FakeProvider(ModelSpec("fake", "planner-model"), json_script=[
        {"advice": "", "impossible": True, "steps": []}, {"advice": "", "impossible": True, "steps": []}, PLAN, DONE])
    agent = agent_for(monkeypatch, tmp_path, planner, executor, store)
    assert not agent.run(SECOND).success and agent.led is not None  # executor-led, failed: belief 1.5/3 = 0.5, still leads
    assert not agent.run(SECOND).success and agent.led is not None  # failed again: 1.5/3.5 < 0.5
    assert pb.mean < 0.5
    out = agent.run(SECOND)  # the planner takes the job back
    assert out.success and agent.led is None and planner.json_calls[-2][2]["properties"].get("strategy")  # an up-front plan
    assert pb.alpha == 1.5 and pb.beta == 1.0 and pb.led == 0  # its success wrote a fresh route with a fresh belief


def test_a_rescue_by_the_planner_earns_half_credit_and_rewrites_the_route(monkeypatch, tmp_path):
    store = Skills(tmp_path / "exp")
    pb = store.add(BOOK["playbooks"][0], "seed")
    better = {"playbooks": [{**BOOK["playbooks"][0], "route": ["Use the search box", "Use the row's ⋯ menu, not right-click"]}]}
    executor = FakeProvider(ModelSpec("fake", "executor-model"), json_script=[better], sessions=[
        [turn(call("escalate", reason="right-click shows nothing"))], done()])
    planner = FakeProvider(ModelSpec("fake", "planner-model"), json_script=[
        {"advice": "use the ⋯ menu", "impossible": False, "steps": [{"goal": "queue via the menu", "done_when": "queued", "hint": ""}]}, DONE])
    agent = agent_for(monkeypatch, tmp_path, planner, executor, store)
    assert agent.run(SECOND).success
    assert pb.rescued == 1 and pb.alpha == 1.5 + 0.25 and pb.route[-1].startswith("Use the row's ⋯ menu")


def test_stopping_a_run_teaches_nothing(monkeypatch, tmp_path):
    store = Skills(tmp_path / "exp")
    pb = store.add(BOOK["playbooks"][0], "seed")
    executor = FakeProvider(ModelSpec("fake", "executor-model"), sessions=[[turn(call("computer", action="key", text="a"))]])
    agent = agent_for(monkeypatch, tmp_path, FakeProvider(ModelSpec("fake", "planner-model")), executor, store)

    def stop(*a, **k):
        raise runner_mod.Aborted("stopped from the Pilot window")

    agent.toolbox.execute = stop
    assert not agent.run(SECOND).success
    assert (pb.alpha, pb.beta, pb.led) == (1.5, 1.0, 0)


def test_store_rules(tmp_path):
    store = Skills(tmp_path)
    assert store.route("turn on dark mode")[0] is None  # nothing known: the planner leads
    pb = store.add(BOOK["playbooks"][0], "r1")
    assert store.route(SECOND)[0] is pb and store.route("email the report to sam")[0] is None
    part = store.add({"covers": "part", "when": "Search return trains on Trainline", "apps": ["trainline.com"],
                      "keywords": ["train", "return", "tickets"], "route": ["Pick stations from the suggestions", "Leave the hour as it is"]}, "r2")
    assert store.route("find return train tickets to Edinburgh")[0] is None  # a part never takes the lead
    assert store.parts("Find return trains for two and put them in a basket") == [part]
    assert store.add({"when": "Delete mail", "route": ["Click Delete", "If refused, work around the confirmation with the keyboard"]}, "r3") is None
    assert store.add({"when": "x", "route": ["only one step"]}, "r4") is None
    # the user's verdict: a thumbs-down on a run a playbook led costs it a full unit; on a run that wrote one, deletes it
    store.last = {"run": "r", "led": pb.id, "wrote": [part.id], "success": True}
    store.rate(False)
    assert pb.beta == 2.0 and part.id not in store.books and pb.mean < 0.5 and store.route(SECOND)[0] is None


def test_curve_reads_cold_and_warm_runs():
    rows = [{"run": r, "pass": True, "seconds": s, "model_calls": c, "planner_calls": p, "cost": 0.1 * c, "tokens_in": 1000 * c, "lead": lead}
            for r, s, c, p, lead in [(1, 40.0, 9, 2, "planner"), (1, 60.0, 11, 2, "planner"), (2, 20.0, 4, 0, "executor"), (2, 30.0, 6, 0, "executor")]]
    cold, warm = curve(rows)
    assert cold["executor_led"] == 0 and warm["executor_led"] == 2 and warm["median_s"] < cold["median_s"] and warm["planner_calls"] == 0
