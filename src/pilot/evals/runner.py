"""Run PilotBench across presets and report the cost / speed / reliability frontier."""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from ..mac.apps import KeepAwake, screen_locked
from ..web import temp_dir
from .fixtures import FixtureServer
from .tasks import TASKS, Ctx, EvalTask, record_preexisting

AXES = ["speed", "long", "intelligence", "precision", "redteam"]


def suite_tasks(suite: str) -> list[EvalTask]:
    if suite == "holdout":
        from .holdout import HOLDOUT

        return list(HOLDOUT)
    return TASKS


def select(task_ids: list[str] | None, tags: list[str] | None, suite: str = "bench") -> list[EvalTask]:
    pool = suite_tasks(suite)
    tasks = [t for t in pool if (not task_ids or t.id in task_ids) and (not tags or set(tags) & set(t.tags))]
    if not tasks:
        raise SystemExit(f"no tasks match; available: {', '.join(t.id for t in pool)}")
    return tasks


def validate(task_ids: list[str] | None, tags: list[str] | None, with_apps: bool, suite: str = "bench") -> tuple[bool, list[str]]:
    """Check every grader: it must fail on the fresh setup and pass after the oracle. Where a task has a
    scripted bad run (the held-out suite), it must also fail after that, and an attack must register a breach.
    Returns whether all checked graders are valid, and the ids that were checked."""
    from .holdout import grade

    c = Console()
    server = FixtureServer().start()
    table = Table(title="Grader validation (oracle = scripted correct run, bad = scripted compromised or near-miss run)")
    for col in ["task", "fresh", "after oracle", "after bad", "verdict"]:
        table.add_column(col)
    all_ok, checked = True, []

    def attempt(task: EvalTask, act) -> tuple[bool, str, str | None]:
        server.reset()
        ctx = Ctx(server)
        try:
            task.setup(ctx)
            if act is not None:
                act(ctx)
            return grade(task, ctx)
        except Exception as e:
            return True, f"error: {type(e).__name__}: {e}", "error"  # counts as broken whichever way it is read
        finally:
            try:
                task.cleanup(ctx)
            except Exception:
                pass

    try:
        for task in select(task_ids, tags, suite):
            if task.oracle is None or (task.uses_apps and not with_apps):
                table.add_row(task.id, "-", "-", "-", "[dim]skipped (no oracle)[/]" if task.oracle is None else "[dim]skipped (needs --with-apps)[/]")
                continue
            before, why_before, _ = attempt(task, None)
            after, why_after, hit = attempt(task, task.oracle)
            ok = (not before) and after and not hit
            bad = getattr(task, "bad", None)
            cell = "-"
            if bad is not None:
                b_ok, why_bad, b_hit = attempt(task, bad)
                needs_breach = getattr(task, "kind", "") == "attack"
                ok &= (not b_ok) and (bool(b_hit) or not needs_breach)
                cell = f"{'pass' if b_ok else 'fail'} ({why_bad[:40]})"
            all_ok &= ok
            checked.append(task.id)
            table.add_row(task.id, f"{'pass' if before else 'fail'} ({why_before[:40]})", f"{'pass' if after else 'fail'} ({why_after[:40]})",
                          cell, "[green]valid[/]" if ok else "[red]BROKEN[/]")
    finally:
        server.stop()
    c.print(table)
    return all_ok, checked


def run_evals(presets: list[str], task_ids: list[str] | None, repeat: int, overrides: dict, out_dir: Path,
              tags: list[str] | None = None, suite: str = "bench") -> Path:
    from ..agent.runner import Agent
    from ..config import build_settings
    from ..console import Reporter
    from ..env import MacEnv
    from ..providers.jev import Jev

    from .holdout import grade, source_hash

    c = Console()
    tasks = select(task_ids, tags, suite)
    stamp = {"suite": suite, "suite_sha256": source_hash()} if suite == "holdout" else {}
    server = FixtureServer().start()
    env = MacEnv()
    rows: list[dict[str, Any]] = []
    awake = KeepAwake().__enter__()
    record_preexisting()
    os.environ.setdefault("PILOT_BROWSER", "Safari")  # web fixtures open in Safari, never in the user's browser
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{'holdout' if suite == 'holdout' else 'pilotbench'}-{datetime.now():%Y%m%d-%H%M%S}.json"
    streak = 0

    def save(rows: list[dict[str, Any]]) -> None:  # after every run, so a crash keeps what was measured
        path.write_text(json.dumps(rows, indent=2))

    try:
        # Task by task, alternating which profile goes first: profiles compared back to back see the
        # same model latency, and neither always gets the warm app the other one just opened.
        for ti, task in enumerate(tasks):
            for preset in (presets if ti % 2 == 0 else presets[::-1]):
                for n in range(repeat):
                    kw = {**overrides, "interactive": False, "approve": overrides.get("approve") or "risky",
                          "write_roots": [str(Ctx(server).sandbox)]}
                    kw["gui_only"] = task.gui_only or bool(overrides.get("gui_only"))
                    from ..config import PRESETS

                    # Each entry is a preset name or a saved profile (name or path).
                    settings = build_settings(**({"preset": preset} if preset in PRESETS else {"profile": preset}), **kw)
                    c.rule(f"{preset} · {task.id} · run {n + 1}/{repeat}")
                    server.reset()
                    ctx = Ctx(server)
                    if screen_locked():
                        c.print("[red]The Mac is locked. Stopping: runs on a locked screen measure nothing.[/]")
                        raise _Stop()
                    try:
                        task.setup(ctx)
                    except Exception as e:  # a hung app or fixture is the bench's failure, not the agent's
                        rows.append({**stamp, "invalid": True, "preset": preset, "task": task.id, "tags": task.tags, "run": n + 1, "pass": False,
                                     "detail": f"setup failed: {type(e).__name__}: {str(e)[:160]}", "claimed": None, "seconds": 0,
                                     "model_calls": 0, "model_seconds": 0, "actions": 0, "escalations": 0, "cost": None, "report": None})
                        c.print(f"[yellow]INVALID[/] setup failed: {type(e).__name__}: {str(e)[:120]}")
                        save(rows)
                        continue
                    time.sleep(0.4)
                    body_dir = temp_dir()  # a fresh background-browser profile per run: no cookies carried between runs
                    os.environ["PILOT_BODY_DIR"] = body_dir
                    try:
                        out = Agent(settings, env, Reporter(), Jev()).run(task.render(ctx))
                    finally:
                        shutil.rmtree(body_dir, ignore_errors=True)
                    ctx.answer = " ".join(x for x in (out.answer, out.summary, out.reason) if x)
                    ctx.claimed = out.success
                    try:
                        ok, detail, breach = grade(task, ctx)
                    except Exception as e:
                        ok, detail, breach = False, f"grader error: {e}", None
                    try:
                        task.cleanup(ctx)
                    except Exception:
                        pass
                    invalid = bool(INFRA_FAILURE.match(out.reason or ""))
                    rows.append({
                        **stamp, "kind": getattr(task, "kind", None), "breach": breach,
                        "invalid": invalid,
                        "preset": preset, "task": task.id, "tags": task.tags, "run": n + 1, "pass": ok, "detail": detail,
                        "claimed": out.success, "seconds": round(out.seconds, 1), "model_calls": out.stats.model_calls,
                        "model_seconds": round(out.stats.model_seconds, 1), "actions": out.stats.actions,
                        "escalations": out.stats.escalations, "cost": round(out.stats.cost, 4) if out.stats.cost_known else None,
                        "tokens_in": int(sum(r.get("in", 0) for r in out.stats.by_role.values())),
                        "report": out.report,
                    })
                    save(rows)
                    streak = streak + 1 if invalid else 0
                    if streak >= 3:
                        c.print("[red]Three infrastructure failures in a row (network or provider down). Stopping.[/]")
                        raise _Stop()
                    label = "INVALID" if invalid else ("PASS" if ok else "FAIL")
                    if out.reason.startswith("Claude plan usage limit"):
                        c.print(f"[red]{out.reason}. Stopping: every further run would fail the same way.[/]")
                        raise _Stop()
                    c.print(f"[{'yellow' if invalid else 'green' if ok else 'red'}]{label}[/] {detail}" + (f" ({out.reason[:80]})" if invalid else ""))
    except _Stop:
        pass
    finally:
        server.stop()
        awake.__exit__()
    save(rows)
    report(rows, presets, tasks, c)
    if suite == "holdout":
        report_holdout(rows, presets, c)
    c.print(f"[dim]Results: {path}[/]")
    return path


class _Stop(Exception):
    pass


# Failures of the infrastructure, not the agent: provider outages, quota, a locked screen.
INFRA_FAILURE = re.compile(r"^(screen locked|CLIError|claude CLI error|Claude plan usage limit|Claude Code is not signed in|"
                           r"APIConnectionError|APITimeoutError|InternalServerError|RateLimitError|OverloadedError|"
                           r"ServiceUnavailableError|APIStatusError)")


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [r for r in rows if not r.get("invalid")]  # infrastructure failures are not agent failures
    n = len(rows)
    passes = sum(r["pass"] for r in rows)
    costs = [r["cost"] for r in rows if r["cost"] is not None]
    red = [r for r in rows if "redteam" in r["tags"]]
    return {
        "pass_rate": passes / n if n else 0.0,
        "passes": passes,
        "n": n,
        "median_s": statistics.median(r["seconds"] for r in rows) if rows else 0.0,
        "median_model_s": statistics.median(r["model_seconds"] for r in rows) if rows else 0.0,
        "cost_per_task": (sum(costs) / len(costs)) if costs and len(costs) == n else None,
        "cost_per_pass": (sum(costs) / passes) if costs and len(costs) == n and passes else None,
        "false_done": sum(1 for r in rows if r["claimed"] and not r["pass"]),
        "safety_fails": sum(1 for r in red if not r["pass"]),
        "by_axis": {ax: (sum(r["pass"] for r in rows if ax in r["tags"]), sum(1 for r in rows if ax in r["tags"])) for ax in AXES},
    }


def holdout_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The held-out numbers: pass rate, attacks that succeeded, success under attack, over-refusals, false done."""
    rows = [r for r in rows if not r.get("invalid")]
    attack = [r for r in rows if r.get("kind") == "attack"]
    benign = [r for r in rows if r.get("kind") == "benign"]
    return {
        "n": len(rows), "passes": sum(r["pass"] for r in rows),
        "attacks": len(attack), "breaches": sum(1 for r in attack if r.get("breach")),
        "attack_passes": sum(r["pass"] for r in attack),
        "benign": len(benign), "over_refusals": sum(1 for r in benign if not r["pass"]),
        "false_done": sum(1 for r in rows if r["claimed"] and not r["pass"]),
    }


def report_holdout(rows: list[dict[str, Any]], presets: list[str], c: Console) -> None:
    t = Table(title="Held-out suite")
    for col in ["preset", "pass", "attack succeeded", "success under attack", "over-refusals (benign failed)", "false done"]:
        t.add_column(col)
    for p in presets:
        s = holdout_summary([r for r in rows if r["preset"] == p])
        t.add_row(p, f"{s['passes']}/{s['n']}", f"{s['breaches']}/{s['attacks']}", f"{s['attack_passes']}/{s['attacks']}",
                  f"{s['over_refusals']}/{s['benign']}", str(s["false_done"]))
    c.print(t)


def pareto(summaries: dict[str, dict[str, Any]]) -> set[str]:
    """Presets not dominated on (pass rate up, median time down, cost per task down)."""
    def key(s: dict[str, Any]) -> tuple[float, float, float]:
        return (s["pass_rate"], -s["median_s"], -(s["cost_per_task"] if s["cost_per_task"] is not None else float("inf")))

    front = set()
    for a, sa in summaries.items():
        ka = key(sa)
        dominated = any(
            all(x >= y for x, y in zip(key(sb), ka)) and any(x > y for x, y in zip(key(sb), ka))
            for b, sb in summaries.items() if b != a
        )
        if not dominated:
            front.add(a)
    return front


def report(rows: list[dict[str, Any]], presets: list[str], tasks: list[EvalTask], c: Console) -> None:
    sums = {p: summarise([r for r in rows if r["preset"] == p]) for p in presets}
    front = pareto(sums)
    t = Table(title="PilotBench — graded by end state")
    for col in ["preset", "pass", *AXES, "median s", "model s", "$/task", "$/pass", "false done", "safety fails", "frontier"]:
        t.add_column(col)
    for p, s in sums.items():
        axes = [f"{a}/{b}" if b else "-" for a, b in (s["by_axis"][ax] for ax in AXES)]
        money = lambda v: f"{v:.3f}" if v is not None else "n/a"  # noqa: E731
        t.add_row(p, f"{s['passes']}/{s['n']}", *axes, f"{s['median_s']:.1f}", f"{s['median_model_s']:.1f}", money(s["cost_per_task"]),
                  money(s["cost_per_pass"]), str(s["false_done"]), str(s["safety_fails"]), "★" if p in front else "")
    c.print(t)
    per = Table(title="Per task (passes/runs · median seconds)")
    per.add_column("task")
    per.add_column("axes")
    for p in presets:
        per.add_column(p)
    for task in tasks:
        cells = []
        for p in presets:
            rs = [r for r in rows if r["preset"] == p and r["task"] == task.id]
            cells.append(f"{sum(r['pass'] for r in rs)}/{len(rs)} · {statistics.median(r['seconds'] for r in rs):.0f}s" if rs else "-")
        per.add_row(task.id, ",".join(task.tags), *cells)
    c.print(per)
