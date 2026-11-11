"""The agent: a slow planner, a fast executor, and harness-side reflexes.

    planner (rare, smart)   plan once -> [escalation -> replan] -> verify at the end
    executor (fast, cheap)  one short, fresh session per step; batches actions
    reflexes (no LLM)       settle detection, auto-observation, no-change and
                            loop detection, AX element resolution, optional Jev checks

Each executor step starts a fresh session, so contexts stay small (fast prefill,
cheap) while the byte-identical system prompt and tools keep prompt caches warm.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import Future
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Settings
from ..console import Reporter
from ..env import MacEnv
from .. import intake
from ..experience import SELF, Experience
from ..mac import apps
from ..providers import cost_of, get_provider
from ..providers.jev import Jev
from ..safety import Aborted, Policy, TaskScope
from ..skills import Skills
from ..tools import MUTATING, OFFSCREEN, Toolbox, build_specs
from ..trace import Trace
from ..types import AssistantTurn, Observation, Part, TextPart, ToolResult
from ..replay.record import DecisionRecorder, history_text

ROTATE_HISTORY = 60  # actions a restarted session is told about
ROTATE_RESULT = 600  # characters kept of each action's result
FOUND_CHARS = 3000  # of a finished step's findings carried into every later step's brief
FRONT_WAIT = 6.0  # seconds the executor's finished first turn waits for the front desk before going on without it
from . import prompts


# Ends that say nothing about how the job should be done: nothing is learned from them, for or against a playbook.
NOT_THE_AGENT = ("aborted:", "interrupted by user", "screen locked", "Claude plan usage limit", "CLIError", "claude CLI error", "Claude Code is not signed in")


class BudgetExceeded(Exception):
    pass


class ScreenLocked(RuntimeError):
    pass


@dataclass
class Step:
    goal: str
    done_when: str = ""
    hint: str = ""
    status: str = "pending"  # pending | current | done | failed
    summary: str = ""
    found: str = ""  # what the step learned (its `answer`): later steps start in a fresh session and need it


@dataclass
class StepResult:
    kind: str  # done | escalate | fail | rebrief (the front desk has answers or a route: start over from them)
    success: bool = False
    summary: str = ""
    answer: str = ""
    reason: str = ""


@dataclass
class Outcome:
    success: bool
    answer: str
    summary: str
    seconds: float
    report: str
    stats: Any
    reason: str = ""
    steps: list[Step] = field(default_factory=list)


# Tasks whose outcome lives on disk; reading an input file ("using ~/x.txt") is not one.
WRITE_TASK = re.compile(r"\b(create|save|write|move|copy|rename|delete|remove|archive|put|make|export|download|store|list(ing)?\s+.*\binto)\b", re.I)


def _tree(root: Path, raw: str, depth: int = 4, folders: int = 60, chars: int = 6000) -> list[str]:
    """Every folder under `root` that holds something, with what it holds: enough to confirm a reorganisation."""
    out: list[str] = []
    size = 0
    stack = [(root, 0)]
    while stack and len(out) < folders and size < chars:
        folder, level = stack.pop(0)
        try:
            kids = sorted(p for p in folder.iterdir() if not p.name.startswith("."))
        except OSError:
            continue
        if folder != root:
            names = [p.name + ("/" if p.is_dir() else "") for p in kids]
            line = f"  {raw.rstrip('/')}/{folder.relative_to(root)}/ : {', '.join(names[:40]) or '(empty)'}" + (f" … and {len(names) - 40} more" if len(names) > 40 else "")
            out.append(line)
            size += len(line)
        if level < depth:
            stack += [(p, level + 1) for p in kids if p.is_dir() and not p.is_symlink()]
    if stack:
        out.append("  (more folders not listed)")
    return out


def evidence(task: str, limit: int = 1500, deep: bool = False) -> str:
    """Read the files and folders the task names straight from disk, for the verifier.

    A screenshot can show one window; the end state of a file task lives on disk. Facts
    the harness reads itself are reliable, unlike the executor's claims. `deep` lists every
    level: a tidy-up into Type/Year folders cannot be confirmed from the top two.
    """
    out = []
    for raw in dict.fromkeys(m.rstrip(".,;:)") for m in re.findall(r"(~/[^\s,;'\"]+|/Users/[^\s,;'\"]+)", task)):
        path = Path(os.path.expanduser(raw))  # labelled as read: a sentence's full stop is not part of the name
        if path.is_file():
            try:
                text = path.read_text(errors="replace")
            except OSError as e:
                text = f"(unreadable: {e})"
            out.append(f"{raw} (file, {path.stat().st_size} bytes):\n{text[:limit]}")
        elif path.is_dir():
            items = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir() if not p.name.startswith("."))
            out.append(f"{raw} (folder, {len(items)} items): {', '.join(items[:60])}")
            if deep:
                out += _tree(path, raw)
                continue
            for sub in [path / i.rstrip("/") for i in items if i.endswith("/")][:4]:
                kids = sorted(p.name for p in sub.iterdir() if not p.name.startswith("."))
                out.append(f"  {raw.rstrip('/')}/{sub.name}/ : {', '.join(kids[:40]) or '(empty)'}")
        else:
            out.append(f"{raw}: does not exist")
    return "\n".join(out)


def _steps(data: dict[str, Any]) -> list[Step]:
    out = []
    for s in data.get("steps") or []:
        if isinstance(s, dict) and s.get("goal"):
            out.append(Step(str(s["goal"]), str(s.get("done_when", "")), str(s.get("hint", ""))))
    return out


class Agent:
    def __init__(self, settings: Settings, env: MacEnv, reporter: Reporter, jev: Jev | None = None,
                 memory: Experience | None = None, defer_learning: bool = False, skills: Skills | None = None):
        self.s = settings
        self.env = env
        self.r = reporter
        self.jev = jev if (jev and jev.available and settings.jev) else None
        self.planner = get_provider(settings.planner, settings.profile.keep_images) if settings.planner else None
        self.executor = get_provider(settings.executor, settings.profile.keep_images)
        for prov in (self.planner, self.executor):
            if prov is not None and hasattr(prov, "on_switch"):
                prov.on_switch = self._switched
        self.space = "pixels" if self.executor.native_computer else settings.executor.coord_space
        if hasattr(env, "configure"):
            env.configure(settings.profile)
        confirm = (lambda prompt: self._waiting(reporter.confirm, prompt)) if settings.interactive else None
        self.policy = Policy(settings.approve, confirm)
        ask: Callable[[str], str] | None = self._ask if settings.interactive else None
        self.told: list[tuple[str, str]] = []  # what the user answered when asked, for the whole run
        self.route: list[str] = []
        self.front: Future | None = None
        self.waited = 0.0
        self._progress = ""
        self.limits = {"minutes": settings.max_minutes, "cost": settings.max_cost, "turns": settings.max_turns}
        self.toolbox = Toolbox(env, settings, self.policy, self.space, self.jev, ask)
        g = env.geometry
        self.tools = build_specs(settings, self.space, (g.model_w, g.model_h), planner_mode=self.planner is not None, ax=env.use_ax)
        self.toolbox.schemas = {t.name: t.schema for t in self.tools}
        self.web = settings.profile.web_body
        self.system = prompts.build_executor(settings.profile.executor_rules, web=self.web)
        self.trace: Trace | None = None
        self.t0 = 0.0
        self.turns = 0
        self.advice = ""
        self.planned = False
        # Lessons from earlier attempts. An eval passes its own store (read-only on the sealed suite) and
        # credits it with the grader's verdict itself; otherwise the run learns from its own verdict.
        mode = settings.profile.lessons
        self.memory = memory if memory is not None else (Experience(mode=mode) if mode != "off" else None)
        self.defer_learning = defer_learning
        self.lessons = ""
        # Playbooks decide who leads each task; an eval passes its own store and scores it with the grader's verdict.
        self.skills = skills if skills is not None else (Skills(mode=settings.playbooks) if settings.playbooks != "off" else None)
        self.led: Any = None  # the playbook the executor is leading from, if any
        self.worked = 0.0

    # -- bookkeeping -------------------------------------------------------------------
    def _switched(self, message: str) -> None:
        self.r.info(message)
        if self.trace is not None:
            self.trace.log("note", message=message)

    def _check_budget(self) -> None:
        st, lim = self.trace.stats, self.limits
        over = None
        if time.monotonic() - self.t0 - self.waited > lim["minutes"] * 60:  # time spent waiting for the user is not work
            over = ("minutes", self.s.max_minutes, f"time limit of {lim['minutes']:g} min reached")
        elif st.cost_known and st.cost > lim["cost"]:
            over = ("cost", self.s.max_cost, f"cost limit of ${lim['cost']:g} reached")
        elif self.turns >= lim["turns"]:
            over = ("turns", self.s.max_turns, f"limit of {lim['turns']} executor turns reached")
        if over is None:
            return
        key, more, what = over
        # A long job stops at its limit only if nobody is there to say "keep going": the limit is the
        # user's, so the user may raise it, one allowance at a time.
        if self.s.interactive and self._waiting(self.r.keep_going, what, self._progress):
            lim[key] += more
            self.trace.log("budget_extended", limit=key, now=lim[key])
            return
        raise BudgetExceeded(what)

    # -- the user ----------------------------------------------------------------------
    def _waiting(self, fn: Callable[..., Any], *args: Any) -> Any:
        t = time.monotonic()
        try:
            return fn(*args)
        finally:
            self.waited += time.monotonic() - t

    def _ask(self, question: str, default: str = "") -> str:
        """Ask the user, and keep the answer for the rest of the run: later steps, plans and checks all see it."""
        q = question.strip()
        for asked, answer in self.told:
            if asked == q:
                return answer  # the same question twice (models repeat a call within a turn): asked once
        answer = str(self._waiting(self.r.ask, q, default) or "").strip()
        self.told.append((q, answer))
        # Places the user typed themselves widen where Pilot may work; nothing they say here widens what it may
        # send, buy or delete (an answer like "don't delete anything" must never read as permission).
        self.policy.scope.add_places(answer)
        if self.trace is not None:
            self.trace.log("asked", question=q, answer=answer, default=default)
        return answer

    def goal(self, task: str) -> str:
        return intake.clarified(task, self.told)

    def _open_front_desk(self, task: str, context: str) -> None:
        self.front = None
        if not (self.s.interactive and self.s.intake):
            return  # nobody to ask (evals, scripts), or the user said not to
        fut: Future = Future()

        def work() -> None:
            try:
                fut.set_result(intake.assess(self.executor, task, context))
            except BaseException as e:  # noqa: BLE001 - handed to whoever settles the verdict
                fut.set_exception(e)

        thread = threading.Thread(target=work, name="pilot-front-desk", daemon=True)  # never keeps a finished run alive
        thread.start()
        if self.s.executor.provider == "mlx":
            thread.join()  # one local GPU: no second request alongside the executor
        self.front = fut

    def _front_desk(self) -> bool:
        """Settle the front desk's verdict. True when the run must start over from a fuller brief:
        the user's answers, or a route for a big job."""
        fut, self.front = self.front, None
        if fut is None:
            return False
        try:
            brief, turn = fut.result(timeout=FRONT_WAIT)
        except Exception as e:  # slow or failed: a clear task must never wait on it
            self.trace.log("error", where="intake", error=f"{type(e).__name__}: {e}"[:300])
            return False
        self._record("intake", turn)
        self.trace.log("intake", **brief.to_dict())
        if brief.ready and not brief.route:
            return False
        self.r.route(brief.goal, brief.route)
        for q, default in brief.questions:
            self._ask(q, default)
        self.route = brief.route
        return True

    def _record(self, role: str, turn: AssistantTurn) -> AssistantTurn:
        c = cost_of(turn.model.split("@")[0] if turn.model else "", turn.usage)
        if c is None:
            spec = self.s.planner if role not in {"executor", "intake"} and self.s.planner else self.s.executor
            c = cost_of(spec.model, turn.usage)
        self.trace.stats.model(role, turn.latency, c, self.used(turn), turn.usage.output_tokens)
        self.trace.log(
            "model", role=role, model=turn.model, latency=round(turn.latency, 3), stop=turn.stop, text=turn.text[:2000],
            notes=turn.notes, calls=[{"name": c_.name, "args": c_.args} for c_ in turn.calls],
            **{"in": self.used(turn), "out": turn.usage.output_tokens, "cache_read": turn.usage.cache_read,
                 "cache_write": turn.usage.cache_write, "ctx": turn.context},
        )
        self.r.model(role, turn.latency, turn.text)
        return turn

    def observe(self, label: str) -> Observation:
        t = time.monotonic()
        obs = self.env.observe(settle=True, coord_space=self.space, with_ax=self.env.use_ax)
        self.trace.stats.observe_seconds += time.monotonic() - t
        name = self.trace.image(obs.image, label)
        self.cur_image, self.cur_text = name, obs.text()
        self.trace.log("observe", image=name, app=obs.app, window=obs.window, capture_ms=round(obs.capture_ms), ax_ms=round(obs.ax_ms),
                       elements=obs.outline.count("\n[") + (1 if obs.outline.startswith("[") else 0))
        return obs

    def screen_parts(self, obs: Observation, lead: str) -> list[Part]:
        # Instructions first, then the image, then the element list, then the background page if one is open.
        parts: list[Part] = [TextPart(lead), obs.image, TextPart(obs.text())]
        if (page := self.toolbox.web_view()) is not None:
            parts.append(TextPart(page))
        return parts

    # -- planner ---------------------------------------------------------------------------
    def _plan_call(self, system: str, lead: str, schema: dict[str, Any], label: str) -> dict[str, Any] | None:
        assert self.planner is not None
        obs = self.observe(label)
        system = prompts.planning(system, self.web)
        try:
            data, turn = self.planner.complete_json(system, self.screen_parts(obs, lead), schema)
        except Exception as e:
            self.trace.log("error", where=label, error=f"{type(e).__name__}: {e}")
            self.r.info(f"{label} failed ({type(e).__name__}: {str(e)[:160]}); continuing without it")
            return None
        self._record("planner", turn)
        self.trace.log(label, **data)
        return data

    def _log_text(self, steps: list[Step]) -> str:
        lines = []
        for i, s in enumerate(steps, 1):
            lines.append(f"{i}. [{s.status}] {s.goal}" + (f" — {s.summary}" if s.summary else ""))
        return "\n".join(lines)

    # -- executor -------------------------------------------------------------------------
    def brief(self, task: str, steps: list[Step], i: int, context: str) -> str:
        if self.planner is None:
            text = f"Task: {task}"
        elif not self.planned:
            text = (f"Task: {task}\nDo the whole task, then call `done`. If you get stuck or the task needs "
                    "a multi-step strategy you are unsure of, call `escalate` and a planner will help.")
        else:
            step = steps[i]
            plan = "\n".join(
                f"  {n}. {'[done] ' if s.status == 'done' else '[current] ' if n - 1 == i else ''}{s.goal}"
                + (f" — {s.summary}" if s.status == "done" and s.summary else "")
                # Only with a person present, so eval runs stay comparable with the recorded ones until a
                # paired run on the suite shows this helps there too.
                + (f"\n     Found: {s.found}" if self.s.interactive and s.status == "done" and s.found else "")
                for n, s in enumerate(steps, 1)
            )
            text = (f"Overall task: {task}\nPlan:\n{plan}\n\nDo step {i + 1} now: {step.goal}\n"
                    f"Done when: {step.done_when or 'the goal is visibly achieved'}\n"
                    + (f"Hint: {step.hint}\n" if step.hint else "")
                    + (f"Planner advice: {self.advice}\n" if self.advice else "")
                    + "Do only this step, then call `done`. Escalate if the step is impossible or the plan is wrong.")
        if self.route:
            if not self.planned:  # a single-model agent keeps one step; the route it showed the user still binds it
                text += "\nRoute shown to the user:\n" + "\n".join(f"  {n}. {g}" for n, g in enumerate(self.route, 1))
            text += ("\nThe user is at the keyboard. When a step says to show them something and wait for their yes, "
                     "show it with `ask_user` and go no further without a yes. What is on screen now may have nothing to "
                     "do with the task: open the app or site the step needs yourself.")
        if context:
            text = f"Earlier in this session: {context}\n\n{text}"
        if self.lessons:
            text += "\n\n" + self.lessons
        return text + "\n\nCurrent screen:"

    @staticmethod
    def used(turn: AssistantTurn) -> int:
        u = turn.usage
        return u.input_tokens + u.cache_read + u.cache_write

    @classmethod
    def context(cls, turn: AssistantTurn) -> int:
        """How big the model's context is now: what a restart could shrink (not what the turn cost)."""
        return turn.context or cls.used(turn)

    def _spare(self) -> Any:
        """A session started ahead of need, so a restart doesn't wait for the CLI to boot."""
        spare, self.standby = self.standby, None
        return spare or self.executor.session(self.system, self.tools)

    def _warm(self, turn: AssistantTurn, fresh_in: int = 0) -> None:
        budget = self.s.profile.context_budget
        if budget and self.standby is None and self.context(turn) > 0.6 * budget and self.context(turn) - fresh_in >= 0.3 * budget:
            self.standby = self.executor.session(self.system, self.tools)  # boots while the actions run

    def rotated_lead(self, brief: str, notes: list[str]) -> str:
        """The step brief, every action so far with its result, and pending harness notes: what a
        fresh session needs to carry on. Old screenshots and element lists are dropped (ids are
        renumbered every turn, so old lists can only mislead)."""
        head = brief.removesuffix("\n\nCurrent screen:")
        # The whole step, not the last few actions: the first ones are often the reads that carry the
        # task's data (a restart once dropped `cat applicant.txt` and re-read it, disoriented).
        hist = history_text({"history": self.recorder.history[-ROTATE_HISTORY:]}, result_chars=ROTATE_RESULT)
        text = head + ("\n\nActions so far in this step (most recent last):\n" + hist if hist else "")
        text += "".join(f"\n{n}" for n in notes)
        return text + "\n\nCurrent screen:"

    def run_step(self, task: str, steps: list[Step], i: int, obs: Observation, context: str) -> StepResult:
        planner_mode = self.planner is not None
        session = self._spare()

        def on_note(n: str) -> None:
            self.r.note(n)

        session.on_note = on_note
        self._check_budget()
        brief = self.brief(task, steps, i, context)
        rec = self.recorder
        rec.begin_step(brief, steps[i].goal)
        rec.before(self.cur_image, self.cur_text)
        turn = self._record("executor", session.send(self.screen_parts(obs, brief)))
        rec.after(turn)
        if self.front is not None and self._front_desk():
            # The front desk was thinking while the executor took its first look. Nothing of that turn has
            # been executed: drop it and start again from the answers.
            rec.flush()
            if hasattr(session, "close"):
                session.close()
            return StepResult("rebrief")
        fresh_in = self.context(turn)  # what a restart costs anyway: only growth beyond it can be saved
        self.turns += 1
        step_turns = 1
        nudged = False
        evidence_shown = False
        prev_hash = obs.hash
        prof = self.s.profile
        recent: deque[str] = deque(maxlen=max(prof.loop_window, 1))
        while True:
            if turn.stop == "refusal":
                return StepResult("fail", reason=f"model refused: {turn.detail}")
            if not turn.calls:
                if not nudged and prof.nudge_on_text_end:
                    nudged = True
                    self._check_budget()
                    msg = "Continue: act with tools, or call `done`" + (" or `escalate`." if planner_mode else ".")
                    self.trace.log("scaffold", name="nudge")
                    rec.flush()
                    rec.before(self.cur_image, self.cur_text, [msg])
                    turn = self._record("executor", session.send([TextPart(msg)]))
                    rec.after(turn)
                    self.turns += 1
                    continue
                rec.flush()
                return StepResult("done", success=True, summary=turn.text[:500], answer="")

            results: list[ToolResult] = []
            failed = False
            mutated = False
            self.toolbox.signal = None
            for call in turn.calls:
                if failed:
                    results.append(ToolResult(call.id, False, "Not executed: an earlier action in this turn failed."))
                    continue
                t = time.monotonic()
                res = self.toolbox.execute(call)
                dt = time.monotonic() - t
                is_observe = call.name == "computer" and call.args.get("action") in {"screenshot", "zoom"}
                if is_observe:
                    self.trace.stats.observe_seconds += dt
                else:
                    self.trace.stats.action_seconds += dt
                    self.trace.stats.actions += call.name not in {"done", "escalate"}
                ev: dict[str, Any] = {"name": call.name, "args": call.args, "ok": res.ok, "result": res.text[:1000], "seconds": round(dt, 3)}
                if res.image is not None:
                    ev["image"] = self.trace.image(res.image, call.args.get("action", call.name))
                    if res.observation:  # a screenshot: this is now what the model sees
                        self.cur_image, self.cur_text = ev["image"], res.observation
                self.trace.log("action", **ev)
                for sev in self.toolbox.events:  # scaffold activations inside tools (e.g. waiting for an editable field)
                    self.trace.log("scaffold", **sev)
                self.toolbox.events.clear()
                if call.name not in {"done", "escalate"}:
                    self.r.action(call.name, call.args, res.ok, res.text, dt)
                results.append(res)
                failed = not res.ok
                if call.name not in OFFSCREEN:
                    mutated = mutated or call.name in MUTATING or call.args.get("action") in MUTATING

            # One view of the background page per turn, on its last web action: earlier ones would be stale.
            ran_web = [r for c, r in zip(turn.calls, results) if c.name == "web" and not r.text.startswith("Not executed")]
            if ran_web and ran_web[-1].image is None and (page := self.toolbox.web_view()) is not None:
                ran_web[-1].text += "\n\n" + page
            rec.results(turn.calls, results)
            signal = self.toolbox.signal
            if signal and signal[0] == "escalate":
                return StepResult("escalate", reason=str(signal[1].get("reason", ""))[:600])
            if signal and signal[0] == "done":
                args = signal[1]
                rejected = self._reflex_reject(steps[i]) if planner_mode and args.get("success") is not False else None
                if rejected is None and args.get("success") is True and not evidence_shown and self.s.profile.evidence_gate and WRITE_TASK.search(task):
                    # The screen can show an uncommitted edit (a rename field reading "Pilot Folder")
                    # while nothing changed on disk. One cheap executor turn against the real state
                    # catches that before the task is reported done.
                    ev = evidence(task, deep=self.s.interactive)  # deeper only with a person present: eval runs stay comparable
                    if ev:
                        evidence_shown = True
                        rejected = ("Before finishing, check the real end state. Read from disk just now:\n" + ev +
                                    "\nIf this shows the task is done, call `done` again; if not, fix it first.")
                        self.trace.log("evidence_gate", evidence=ev[:1500])
                if rejected is None:
                    return StepResult("done", success=args.get("success") is not False, summary=str(args.get("summary", ""))[:600],
                                      answer=str(args.get("answer") or ""))
                for r in results:
                    if r.text == "Recorded.":
                        r.ok, r.text = False, rejected

            if apps.screen_locked():
                raise ScreenLocked("the Mac locked during the run")
            step_turns += 1
            if planner_mode and step_turns > self.s.profile.max_step_turns:
                return StepResult("escalate", reason=f"no completion after {self.s.profile.max_step_turns} turns on this step")
            extra: list[Part] = []
            latest: Observation | None = None
            took_shot = any(r.image is not None for r in results)
            if self.s.profile.auto_observe and mutated and not took_shot:
                new = latest = self.observe("after")
                target = results[-1] if results and results[-1].ok and results[-1].image is None else None
                if target is not None:
                    target.image, target.observation = new.image, "Screen after your actions:\n" + new.text()
                else:
                    extra += [TextPart("Screen after your actions:"), new.image, TextPart(new.text())]
                if new.hash == prev_hash and prof.no_change_note:
                    extra.append(TextPart("Note: the screen did not visibly change after these actions."))
                    self.trace.log("scaffold", name="no_change_note")
                prev_hash = new.hash
            sig = json.dumps([(c.name, c.args) for c in turn.calls], sort_keys=True, default=str)
            recent.append(sig)
            if prof.loop_window and len(recent) == prof.loop_window and len(set(recent)) == 1:
                times = {2: "twice", 3: "three times", 4: "four times"}.get(prof.loop_window, f"{prof.loop_window} times")
                self.trace.log("scaffold", name="loop_note")
                extra.append(TextPart(f"You have repeated the same actions {times}. They are not working: change approach"
                                      + (" or escalate." if planner_mode else ".")))
            if prof.budget_warning and planner_mode and step_turns == prof.max_step_turns - 2:
                extra.append(TextPart("Two turns left for this step: finish with `done` or `escalate`."))
                self.trace.log("scaffold", name="budget_warning")
            self._check_budget()
            notes = [p.text for p in extra if isinstance(p, TextPart)]
            if prof.context_budget and self.context(turn) > prof.context_budget and self.context(turn) - fresh_in >= prof.context_budget / 2:
                # Restart from a compact text history and the current screen instead of resending
                # every old screenshot: at 30k+ tokens a call takes about twice as long.
                before = self.context(turn)
                if hasattr(session, "close"):
                    session.close()
                session = self._spare()
                session.on_note = on_note
                latest = latest or self.observe("rotate")
                kept = [n for n in notes if not n.startswith("Screen after your actions")]
                self.trace.log("scaffold", name="context_rotate", tokens=before, history=len(rec.history))
                rec.before(self.cur_image, self.cur_text, kept)
                turn = self._record("executor", session.send(self.screen_parts(latest, self.rotated_lead(brief, kept))))
                fresh_in = self.context(turn)
            else:
                rec.before(self.cur_image, self.cur_text, notes)
                turn = self._record("executor", session.reply(turn.calls, results, extra))
            rec.after(turn)
            self._warm(turn, fresh_in)
            self.turns += 1

    def learn(self, run_dir: Path, success: bool, weight: float, feedback: str = "") -> None:
        """Reflect on a finished run: credit the lessons it followed, keep new ones. Never fails the task."""
        assert self.memory is not None
        t = time.monotonic()
        try:
            entry = self.memory.learn(self.planner or self.executor, run_dir, success, weight, feedback)
        except Exception as e:
            self.r.info(f"learning skipped ({type(e).__name__}: {str(e)[:120]})")
            return
        followed = sum(v == "followed" for v in entry["verdicts"].values())
        self.r.info(f"learned in {time.monotonic() - t:.0f}s: {followed} lesson(s) followed and credited, {len(entry['added'])} new")

    def learn_route(self, run_dir: Path, success: bool, weight: float, feedback: str = "") -> None:
        """Score the playbook that led this run and distil a new one from a success the planner helped with."""
        if self.skills is None or self.skills.mode != "learn":
            return
        st = self.trace.stats
        stats = {"seconds": round(self.worked, 1), "calls": st.model_calls, "planner": st.by_role.get("planner", {}).get("calls", 0)}
        t = time.monotonic()
        entry = self.skills.after(self.executor, run_dir, success, weight, self.led, stats, st.escalations, feedback)
        if entry["wrote"]:
            self.r.info(f"learned in {time.monotonic() - t:.0f}s: {len(entry['wrote'])} playbook(s) for next time")

    def _reflex_reject(self, step: Step) -> str | None:
        """Jev checks the step's visible done-condition; only a confident 'no' blocks."""
        if self.jev is None or not step.done_when:
            return None
        try:
            obs = self.observe("reflex-check")
            ans = self.jev.yes_no(f"Is this true on the screen: {step.done_when}?", obs.image, obs.text()[:4000])
        except Exception as e:
            self.trace.log("error", where="jev", error=str(e))
            return None
        self.trace.log("reflex", check=step.done_when, value=ans.value, confidence=ans.confidence, latency=ans.latency)
        if ans.value is False and ans.confidence >= 0.85:
            return f"Not accepted: a verifier is confident this is not yet true: {step.done_when}. Keep going."
        return None

    # -- orchestration ---------------------------------------------------------------------
    def run(self, task: str, context: str = "") -> Outcome:
        s = self.s
        meta = {"planner": s.planner.label if s.planner else None, "executor": s.executor.label, "preset": s.preset,
                "coords": self.space, "ax": self.env.use_ax, "geometry": vars(self.env.geometry)}
        self.trace = Trace(s.runs_dir, task, meta)
        self.cur_image: str | None = None
        self.cur_text: str | None = None
        self.recorder = DecisionRecorder(self.trace, self.env, space=self.space, executor=s.executor.label, system=self.system,
                                         tools=[t.name for t in self.tools], planner_mode=self.planner is not None)
        self.t0 = time.monotonic()
        self.turns = 0
        self.standby: Any = None
        self.advice = ""
        self.planned = False
        self.told, self.route, self.waited = [], [], 0.0
        self.limits = {"minutes": s.max_minutes, "cost": s.max_cost, "turns": s.max_turns}
        self._progress = ""
        # Authorisation comes from the user's own words for this task, never from the screen.
        self.policy.scope = TaskScope.from_task(task, s.write_roots)
        self._open_front_desk(task, context)
        self.lessons = ""
        if self.memory is not None:
            chosen = self.memory.select(task)
            self.lessons = Experience.brief(chosen)
            self.trace.log("lessons", ids=[x.id for x, _ in chosen], relevance=[round(r, 2) for _, r in chosen], mode=self.memory.mode)
        self.r.header(task, s)
        # Who leads: the planner on a new kind of job (it plans from the screen and checks the result), the executor
        # on a kind it holds a playbook for. Without playbooks the profile decides, as before.
        upfront = self.planner is not None and s.profile.plan == "upfront"
        self.led = None
        if self.skills is not None:
            self.led, why = self.skills.route(task)
            if self.planner is not None:
                upfront = self.led is None
            self.trace.log("lead", **why)
            self.r.lead(why)
            book = Skills.brief(self.led, self.skills.parts(task, skip=self.led.id if self.led else ""))
            self.lessons = "\n\n".join(x for x in (self.lessons, book) if x)
        success, answer, summary, reason = False, "", "", ""
        steps: list[Step] = []
        try:
            if apps.screen_locked():
                raise ScreenLocked("the Mac is locked; unlock it and run again (Pilot never types passwords)")
            obs = self.observe("start")
            if upfront:
                self.planned = True
                self._front_desk()  # ask first: the plan is then made from the answers
                self.route = []  # the planner, who sees the screen, writes the steps
                t = time.monotonic()
                data = self._plan_call(prompts.PLANNER, f"Task: {self.goal(task)}\nGUI-only: {s.gui_only}\n" + (f"Earlier in this session: {context}\n" if context else "")
                                       + (f"{self.lessons}\n" if self.lessons else "") + "Current screen:",
                                       prompts.PLAN_SCHEMA, "plan")
                if data and data.get("impossible"):
                    raise RuntimeError(f"Not possible: {data.get('strategy') or 'the planner found no way to do this'}")
                steps = _steps(data or {}) or [Step(task)]
                if data:
                    self.r.plan(data, time.monotonic() - t)
                obs = self.observe("step")
            else:
                steps = [Step(task)]
            i = 0
            verify_rounds = 0
            escalations = 0
            while True:
                while i < len(steps):
                    steps[i].status = "current"
                    if self.planned:
                        self.r.step(i + 1, len(steps), steps[i].goal)
                    self.trace.log("step", index=i, goal=steps[i].goal, done_when=steps[i].done_when)
                    self._progress = self._log_text(steps)
                    res = self.run_step(self.goal(task), steps, i, obs, context)
                    self.trace.log("step_result", index=i, outcome=res.kind, success=res.success, summary=res.summary, reason=res.reason)
                    if res.kind == "rebrief":
                        if self.route and self.planner is not None:
                            # A big job: each milestone gets its own fresh executor session and turn budget.
                            steps, self.planned = [Step(g) for g in self.route], True
                        obs = self.observe("step")
                        continue
                    if res.kind == "done" and res.success:
                        steps[i].status, steps[i].summary, steps[i].found = "done", res.summary, res.answer[:FOUND_CHARS]
                        answer = res.answer or answer
                        summary = res.summary
                        i += 1
                        if i < len(steps):
                            obs = self.observe("step")
                        continue
                    reason = res.reason or res.summary or "step failed"
                    steps[i].status = "failed"
                    if self.planner is None or escalations >= s.profile.max_escalations or res.kind == "fail":
                        raise RuntimeError(reason)
                    escalations += 1
                    self.trace.stats.escalations += 1
                    self.trace.log("escalate", reason=reason)
                    self.r.info(f"↑ escalate: {reason[:160]}")
                    t = time.monotonic()
                    lead = (f"Task: {self.goal(task)}\nGUI-only: {s.gui_only}\n" + (f"Earlier in this session: {context}\n" if context else "") + f"Progress:\n{self._log_text(steps)}\n"
                            f"The executor escalated on step {i + 1}: {reason}\n" + (f"{self.lessons}\n" if self.lessons else "") + "Current screen:")
                    data = self._plan_call(prompts.REPLANNER, lead, prompts.REPLAN_SCHEMA, "replan")
                    if data and data.get("impossible"):
                        raise RuntimeError(f"Not possible: {data.get('advice') or reason}")
                    new = _steps(data or {})
                    if not new:
                        raise RuntimeError(f"stuck: {reason}")
                    self.advice = str((data or {}).get("advice", ""))[:600]
                    self.planned = True
                    self.r.plan(data or {}, time.monotonic() - t, "Replan")
                    steps = steps[:i] + new
                    obs = self.observe("step")
                need_verify = self.planner is not None and (
                    s.profile.verify == "always"
                    or (s.profile.verify == "auto" and (self.planned or escalations > 0 or self.turns > 6))
                )
                if need_verify:
                    t = time.monotonic()
                    ev = evidence(self.goal(task), deep=s.interactive)
                    lead = (f"Task: {self.goal(task)}\n" + (f"Earlier in this session: {context}\n" if context else "") + f"Executor log:\n{self._log_text(steps)}\n"
                            + (f"Answer reported by executor: {answer}\n" if answer else "")
                            + (f"Evidence read from disk by the harness just now (reliable):\n{ev}\n" if ev else "")
                            + "Current screen:")
                    v = self._plan_call(prompts.VERIFIER, lead, prompts.VERIFY_SCHEMA, "verify")
                    if v is None:
                        success = True
                        break
                    self.r.info(f"verify ({time.monotonic() - t:.1f}s): {'complete' if v.get('complete') else 'incomplete — ' + str(v.get('missing'))[:140]}")
                    if v.get("complete"):
                        success = True
                        answer = str(v.get("answer") or "") or answer
                        break
                    more = _steps(v)
                    if verify_rounds >= 1 or not more:
                        reason = str(v.get("missing") or "verifier found the task incomplete")
                        break
                    verify_rounds += 1
                    steps += more
                    obs = self.observe("step")
                    continue
                success = True
                break
        except Aborted as e:
            reason = f"aborted: {e}"
        except KeyboardInterrupt:
            reason = "interrupted by user"
        except BudgetExceeded as e:
            reason = str(e)
        except ScreenLocked as e:
            reason = f"screen locked: {e}"
        except Exception as e:
            reason = f"{type(e).__name__}: {e}" if not isinstance(e, RuntimeError) else str(e)
        seconds = time.monotonic() - self.t0
        self.toolbox.close()
        if self.standby is not None and hasattr(self.standby, "close"):
            self.standby.close()
            self.standby = None
        report = self.trace.finish({"success": success, "answer": answer, "summary": summary, "reason": reason,
                                    "steps": [vars(x) for x in steps]})
        self.r.final(success, answer, summary if success else reason, self.trace.stats, seconds, str(report))
        if s.speak:
            apps.say(answer or ("Done." if success else f"I couldn't finish: {reason}"))
        self.worked = max(seconds - self.waited, 0.0)
        if self.memory is not None and self.memory.mode == "learn" and not self.defer_learning:
            self.learn(self.trace.dir, success, SELF)
        if self.skills is not None and not self.defer_learning and not reason.startswith(NOT_THE_AGENT):
            self.learn_route(self.trace.dir, success, SELF)
        return Outcome(success, answer, summary, seconds, str(report), self.trace.stats, reason, steps)
