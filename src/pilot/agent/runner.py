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
import time
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Settings
from ..console import Reporter
from ..env import MacEnv
from ..mac import apps
from ..providers import cost_of, get_provider
from ..providers.jev import Jev
from ..safety import Aborted, Policy, TaskScope
from ..tools import MUTATING, Toolbox, build_specs
from ..trace import Trace
from ..types import AssistantTurn, Observation, Part, TextPart, ToolResult
from ..replay.record import DecisionRecorder
from . import prompts


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


@dataclass
class StepResult:
    kind: str  # done | escalate | fail
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


def evidence(task: str, limit: int = 1500) -> str:
    """Read the files and folders the task names straight from disk, for the verifier.

    A screenshot can show one window; the end state of a file task lives on disk. Facts
    the harness reads itself are reliable, unlike the executor's claims.
    """
    out = []
    for raw in dict.fromkeys(re.findall(r"(~/[^\s,;'\"]+|/Users/[^\s,;'\"]+)", task)):
        path = Path(os.path.expanduser(raw.rstrip(".,;:)")))
        if path.is_file():
            try:
                text = path.read_text(errors="replace")
            except OSError as e:
                text = f"(unreadable: {e})"
            out.append(f"{raw} (file, {path.stat().st_size} bytes):\n{text[:limit]}")
        elif path.is_dir():
            items = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir() if not p.name.startswith("."))
            out.append(f"{raw} (folder, {len(items)} items): {', '.join(items[:60])}")
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
    def __init__(self, settings: Settings, env: MacEnv, reporter: Reporter, jev: Jev | None = None):
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
        confirm = reporter.confirm if settings.interactive else None
        self.policy = Policy(settings.approve, confirm)
        ask: Callable[[str], str] | None = reporter.ask if settings.interactive else None
        self.toolbox = Toolbox(env, settings, self.policy, self.space, self.jev, ask)
        g = env.geometry
        self.tools = build_specs(settings, self.space, (g.model_w, g.model_h), planner_mode=self.planner is not None, ax=env.use_ax)
        self.toolbox.schemas = {t.name: t.schema for t in self.tools}
        self.system = prompts.build_executor(settings.profile.executor_rules)
        self.trace: Trace | None = None
        self.t0 = 0.0
        self.turns = 0
        self.advice = ""
        self.planned = False

    # -- bookkeeping -------------------------------------------------------------------
    def _switched(self, message: str) -> None:
        self.r.info(message)
        if self.trace is not None:
            self.trace.log("note", message=message)

    def _check_budget(self) -> None:
        st = self.trace.stats
        if time.monotonic() - self.t0 > self.s.max_minutes * 60:
            raise BudgetExceeded(f"time limit of {self.s.max_minutes:g} min reached")
        if st.cost_known and st.cost > self.s.max_cost:
            raise BudgetExceeded(f"cost limit of ${self.s.max_cost:g} reached")
        if self.turns >= self.s.max_turns:
            raise BudgetExceeded(f"limit of {self.s.max_turns} executor turns reached")

    def _record(self, role: str, turn: AssistantTurn) -> AssistantTurn:
        c = cost_of(turn.model.split("@")[0] if turn.model else "", turn.usage)
        if c is None:
            spec = self.s.planner if role != "executor" and self.s.planner else self.s.executor
            c = cost_of(spec.model, turn.usage)
        self.trace.stats.model(role, turn.latency, c, turn.usage.input_tokens + turn.usage.cache_read, turn.usage.output_tokens)
        self.trace.log(
            "model", role=role, model=turn.model, latency=round(turn.latency, 3), stop=turn.stop, text=turn.text[:2000],
            notes=turn.notes, calls=[{"name": c_.name, "args": c_.args} for c_ in turn.calls],
            **{"in": turn.usage.input_tokens + turn.usage.cache_read, "out": turn.usage.output_tokens, "cache_read": turn.usage.cache_read},
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

    @staticmethod
    def screen_parts(obs: Observation, lead: str) -> list[Part]:
        # Instructions first, then the image, then the element list.
        return [TextPart(lead), obs.image, TextPart(obs.text())]

    # -- planner ---------------------------------------------------------------------------
    def _plan_call(self, system: str, lead: str, schema: dict[str, Any], label: str) -> dict[str, Any] | None:
        assert self.planner is not None
        obs = self.observe(label)
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
                for n, s in enumerate(steps, 1)
            )
            text = (f"Overall task: {task}\nPlan:\n{plan}\n\nDo step {i + 1} now: {step.goal}\n"
                    f"Done when: {step.done_when or 'the goal is visibly achieved'}\n"
                    + (f"Hint: {step.hint}\n" if step.hint else "")
                    + (f"Planner advice: {self.advice}\n" if self.advice else "")
                    + "Do only this step, then call `done`. Escalate if the step is impossible or the plan is wrong.")
        if context:
            text = f"Earlier in this session: {context}\n\n{text}"
        return text + "\n\nCurrent screen:"

    def run_step(self, task: str, steps: list[Step], i: int, obs: Observation, context: str) -> StepResult:
        planner_mode = self.planner is not None
        session = self.executor.session(self.system, self.tools)

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
                if call.name not in {"done", "escalate"}:
                    self.r.action(call.name, call.args, res.ok, res.text, dt)
                results.append(res)
                failed = not res.ok
                mutated = mutated or call.name in MUTATING or call.args.get("action") in MUTATING

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
                    ev = evidence(task)
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
            took_shot = any(r.image is not None for r in results)
            if self.s.profile.auto_observe and mutated and not took_shot:
                new = self.observe("after")
                target = results[-1] if results and results[-1].ok and results[-1].image is None else None
                if target is not None:
                    target.image, target.observation = new.image, "Screen after your actions:\n" + new.text()
                else:
                    extra += [TextPart("Screen after your actions:"), new.image, TextPart(new.text())]
                if new.hash == prev_hash and prof.no_change_note:
                    extra.append(TextPart("Note: the screen did not visibly change after these actions."))
                prev_hash = new.hash
            sig = json.dumps([(c.name, c.args) for c in turn.calls], sort_keys=True, default=str)
            recent.append(sig)
            if prof.loop_window and len(recent) == prof.loop_window and len(set(recent)) == 1:
                times = {2: "twice", 3: "three times", 4: "four times"}.get(prof.loop_window, f"{prof.loop_window} times")
                extra.append(TextPart(f"You have repeated the same actions {times}. They are not working: change approach"
                                      + (" or escalate." if planner_mode else ".")))
            if prof.budget_warning and planner_mode and step_turns == prof.max_step_turns - 2:
                extra.append(TextPart("Two turns left for this step: finish with `done` or `escalate`."))
            self._check_budget()
            rec.before(self.cur_image, self.cur_text, [p.text for p in extra if isinstance(p, TextPart)])
            turn = self._record("executor", session.reply(turn.calls, results, extra))
            rec.after(turn)
            self.turns += 1

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
        self.advice = ""
        self.planned = False
        # Authorisation comes from the user's own words for this task, never from the screen.
        self.policy.scope = TaskScope.from_task(task, s.write_roots)
        self.r.header(task, s)
        success, answer, summary, reason = False, "", "", ""
        steps: list[Step] = []
        try:
            if apps.screen_locked():
                raise ScreenLocked("the Mac is locked; unlock it and run again (Pilot never types passwords)")
            obs = self.observe("start")
            if self.planner and s.profile.plan == "upfront":
                self.planned = True
                t = time.monotonic()
                data = self._plan_call(prompts.PLANNER, f"Task: {task}\nGUI-only: {s.gui_only}\n" + (f"Earlier in this session: {context}\n" if context else "") + "Current screen:",
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
                    res = self.run_step(task, steps, i, obs, context)
                    self.trace.log("step_result", index=i, outcome=res.kind, success=res.success, summary=res.summary, reason=res.reason)
                    if res.kind == "done" and res.success:
                        steps[i].status, steps[i].summary = "done", res.summary
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
                    lead = (f"Task: {task}\nGUI-only: {s.gui_only}\nProgress:\n{self._log_text(steps)}\n"
                            f"The executor escalated on step {i + 1}: {reason}\nCurrent screen:")
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
                    ev = evidence(task)
                    lead = (f"Task: {task}\nExecutor log:\n{self._log_text(steps)}\n"
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
        report = self.trace.finish({"success": success, "answer": answer, "summary": summary, "reason": reason,
                                    "steps": [vars(x) for x in steps]})
        self.r.final(success, answer, summary if success else reason, self.trace.stats, seconds, str(report))
        if s.speak:
            apps.say(answer or ("Done." if success else f"I couldn't finish: {reason}"))
        return Outcome(success, answer, summary, seconds, str(report), self.trace.stats, reason, steps)
