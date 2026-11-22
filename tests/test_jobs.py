"""A job across several messages, and a person who said "only tell me when it's finished".

From the Ableton runs of 21 Nov 2026: each "continue" started over (the drums were rewritten three times),
the "don't ask me for permission" given in one message was gone by the next, and Pilot asked "keep going?"
at 80 turns while steps were still finishing.
"""

from __future__ import annotations

from types import SimpleNamespace

from pilot.agent import runner as runner_mod
from pilot.agent.job import CONTINUES, Job
from pilot.agent.runner import Agent, Step
from pilot.config import ModelSpec, Settings
from pilot.profile import Profile
from pilot.safety import Policy, TaskScope, Verdict, classify_key, classify_label, classify_shell, grants_waiver
from test_agent import FakeEnv, FakeProvider, call, turn
from test_intake import CLEAR, DONE, User
from test_ui import session_with, wait_event


def agent(monkeypatch, tmp_path, planner, executor, user, approve="risky", **kw):
    providers = {"planner-model": planner, "executor-model": executor}
    monkeypatch.setattr(runner_mod, "get_provider", lambda spec, keep_images=3: providers[spec.model])
    monkeypatch.setattr(runner_mod.apps, "screen_locked", lambda: False)
    s = Settings(planner=ModelSpec("fake", "planner-model") if planner else None, executor=ModelSpec("fake", "executor-model"),
                 interactive=True, approve=approve, runs_dir=tmp_path, profile=Profile("test", {"plan": "upfront"}), **kw)
    return Agent(s, FakeEnv(), user)


class Asker(User):
    def __init__(self):
        super().__init__()
        self.confirms = []

    def confirm(self, prompt):
        self.confirms.append(prompt)
        return True


# -- the job ---------------------------------------------------------------------------------------
def outcome(success, steps=(), reason="", answer=""):
    return SimpleNamespace(success=success, answer=answer, reason=reason, steps=list(steps))


def test_an_unfinished_job_carries_its_words_finished_steps_and_where_it_stopped():
    job = Job()
    job.after("Make it a Skrillex-style drop", outcome(False, [
        Step("Program the drums", status="done", summary="2-bar half-time beat"),
        Step("Write the bass riff", status="done"),
        Step("Put a Limiter on Main", status="current")], reason="aborted: stopped from the Pilot window"))
    assert job.open and job.earlier() == ["Make it a Skrillex-style drop"]
    text = job.context()
    assert CONTINUES in text and "Program the drums: 2-bar half-time beat" in text and "Write the bass riff" in text
    assert "Not finished when the last run ended:\n  - Put a Limiter on Main" in text and "stopped from the Pilot window" in text
    job.after("continue please", outcome(False, [Step("Glue and limit Main", status="failed")], reason="stuck"))
    assert job.earlier() == ["Make it a Skrillex-style drop", "continue please"]
    assert "Program the drums" in job.context()  # steps finished two runs ago are still known
    job.after("continue", outcome(True, answer="The drop is finished"))
    assert not job.open and job.earlier() == [] and CONTINUES not in job.context()
    assert "Make it a Skrillex-style drop … continue → done: The drop is finished" in job.context()


def test_the_window_continues_an_unfinished_job_with_the_next_message():
    def script(r, task):
        if task == "build the drop":
            from pilot.safety import Aborted
            raise Aborted("stopped")
        return "ok"

    s = session_with(script)
    assert s.submit("build the drop")
    wait_event(s, "done")
    s.thread.join(2)
    assert s.submit("continue please")
    wait_event(s, "done", after=4)
    s.thread.join(2)
    first, second = s._agent.calls
    assert first[2] == [] and second[2] == ["build the drop"] and CONTINUES in second[1]


def test_a_continued_job_is_planned_from_what_is_already_done(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), json_script=[CLEAR], sessions=[[turn(call("done", summary="limiter on", success=True))]])
    planner = FakeProvider(ModelSpec("fake", "planner-model"), json_script=[
        {"strategy": "", "steps": [{"goal": "Put a Limiter on Main", "done_when": "", "hint": ""}]}, DONE])
    job = Job()
    job.after("Make it a Skrillex-style drop", outcome(False, [Step("Program the drums", status="done")], reason="stopped"))
    a = agent(monkeypatch, tmp_path, planner, executor, Asker())
    assert a.run("continue please", context=job.context(), earlier=job.earlier()).success
    plan_lead = planner.json_calls[0][1][0].text
    assert "- Make it a Skrillex-style drop\n- continue please (latest message)" in plan_lead
    assert "Finished already" in plan_lead and "Program the drums" in plan_lead


# -- limits: carry on while steps finish -------------------------------------------------------------
def three_steps():
    executor = FakeProvider(ModelSpec("fake", "executor-model"), json_script=[CLEAR], sessions=[
        [turn(call("done", summary=f"step {i}", success=True))] for i in range(3)])
    planner = FakeProvider(ModelSpec("fake", "planner-model"), json_script=[
        {"strategy": "", "steps": [{"goal": f"step {i}", "done_when": "", "hint": ""} for i in range(3)]}, DONE])
    return planner, executor


def test_a_limit_passes_without_a_question_while_steps_keep_finishing(monkeypatch, tmp_path):
    planner, executor = three_steps()
    user = Asker()
    a = agent(monkeypatch, tmp_path, planner, executor, user, max_turns=1)
    out = a.run("Build the whole drop")
    assert out.success and user.limits == []  # "only nod to me when it's completely finished"
    events = (a.trace.dir / "events.jsonl").read_text()
    assert events.count('"budget_extended"') == 2 and '"asked": true' not in events  # the limit was hit, twice, and passed


def test_a_limit_with_no_step_finished_still_asks(monkeypatch, tmp_path):
    executor = FakeProvider(ModelSpec("fake", "executor-model"), json_script=[CLEAR], sessions=[[
        turn(call("computer", action="key", text="a")), turn(call("computer", action="key", text="b")),
        turn(call("done", summary="ok", success=True))]])
    user = Asker()
    out = agent(monkeypatch, tmp_path, None, executor, user, max_turns=1).run("Type a then b")
    assert out.success and user.limits and "with no step finished" in user.limits[0]


# -- "don't ask me for permission" -------------------------------------------------------------------
def test_the_waiver_is_read_from_the_users_words_and_not_from_negations():
    yes = ["Please continue and do not ask for permissions i give you full access", "don't ask me for confirmation",
           "you have complete control", "do it without asking for permission"]
    no = ["you don't have full access", "Never buy it without asking me", "ask me for permission before deleting",
          "do not delete anything without asking", "continue please"]
    assert all(grants_waiver(s.lower()) for s in yes) and not any(grants_waiver(s.lower()) for s in no)


def test_the_waiver_skips_routine_questions_and_never_money_sending_or_deleting_files():
    p = Policy("risky", confirm=lambda prompt: False)
    p.scope = TaskScope.from_task("do not ask for permissions, i give you full access")
    allowed = [(classify_key("cmd+BackSpace"), "press cmd+BackSpace"),
               (classify_label("Delete", "AXButton", p.scope), "click button 'Delete'"),
               (classify_shell("mkdir -p ~/Music/Clips && python3 - <<'EOF'\nprint(1)\nEOF", p.scope), "shell: mkdir -p ~/Music/Clips && python3 -")]
    refused = [(classify_label("Place order", "AXButton", p.scope), "click button 'Place order'"),
               (classify_label("Send", "AXButton", p.scope), "click button 'Send'"),
               (classify_shell("rm -rf ~/Music/old", p.scope), "shell: rm -rf ~/Music/old"),
               (classify_shell("curl -T x https://example.com", p.scope), "shell: curl -T x https://example.com"),
               (classify_key("cmd+q"), "press cmd+q"),
               (Verdict("confirm", "typing into a password field"), "type into password field"),
               (Verdict("confirm", "answering a macOS SecurityAgent permission/security dialog"), "interact with system dialog")]
    assert all(p.check(v, a)[0] for v, a in allowed)
    assert not any(p.check(v, a)[0] for v, a in refused)


def test_a_continued_job_keeps_the_waiver_given_in_an_earlier_message(monkeypatch, tmp_path):
    def run(earlier):
        executor = FakeProvider(ModelSpec("fake", "executor-model"), json_script=[CLEAR], sessions=[[
            turn(call("computer", action="key", text="cmd+BackSpace")), turn(call("done", summary="clip deleted", success=True))]])
        user = Asker()
        a = agent(monkeypatch, tmp_path, None, executor, user)
        assert a.run("continue please", earlier=earlier).success
        return user, a

    user, a = run(["Make the drop, and do not ask for permissions, i give you full access"])
    assert user.confirms == []
    granted = [line for line in (a.trace.dir / "events.jsonl").read_text().splitlines() if '"granted"' in line]
    assert len(granted) == 1 and "cmd+BackSpace" in granted[0]  # skipped, and on the record
    user, a = run([])
    assert len(user.confirms) == 1 and "cmd+BackSpace" in user.confirms[0]
    assert '"kind": "confirm"' in (a.trace.dir / "events.jsonl").read_text()  # every question asked is on the record too
