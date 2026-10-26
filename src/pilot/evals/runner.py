"""Run PilotBench across presets and report the cost / speed / reliability frontier."""

from __future__ import annotations

import json
import os
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from ..mac.apps import KeepAwake, screen_locked
from .fixtures import FixtureServer
from .tasks import TASKS, Ctx, EvalTask, record_preexisting

AXES = ["speed", "long", "intelligence", "precision", "redteam"]


def select(task_ids: list[str] | None, tags: list[str] | None) -> list[EvalTask]:
    tasks = [t for t in TASKS if (not task_ids or t.id in task_ids) and (not tags or set(tags) & set(t.tags))]
    if not tasks:
        raise SystemExit(f"no tasks match; available: {', '.join(t.id for t in TASKS)}")
    return tasks


def validate(task_ids: list[str] | None, tags: list[str] | None, with_apps: bool) -> bool:
    """Check every grader: it must fail on the fresh setup and pass after the oracle."""
    c = Console()
    server = FixtureServer().start()
    table = Table(title="Grader validation (oracle = scripted correct solution)")
    for col in ["task", "before oracle", "after oracle", "verdict"]:
        table.add_column(col)
    all_ok = True
    try:
        for task in select(task_ids, tags):
            if task.oracle is None or (task.uses_apps and not with_apps):
                table.add_row(task.id, "-", "-", "[dim]skipped (no oracle)[/]" if task.oracle is None else "[dim]skipped (needs --with-apps)[/]")
                continue
            server.reset()
            ctx = Ctx(server)
            try:
                task.setup(ctx)
                before, why_before = task.check(ctx)
                task.oracle(ctx)
                after, why_after = task.check(ctx)
            except Exception as e:
                before, why_before, after, why_after = True, "", False, f"error: {e}"
            finally:
                try:
                    task.cleanup(ctx)
                except Exception:
                    pass
            ok = (not before) and after
            all_ok &= ok
            table.add_row(task.id, f"{'pass' if before else 'fail'} ({why_before[:40]})", f"{'pass' if after else 'fail'} ({why_after[:40]})",
                          "[green]valid[/]" if ok else "[red]BROKEN[/]")
    finally:
        server.stop()
    c.print(table)
    return all_ok


def run_evals(presets: list[str], task_ids: list[str] | None, repeat: int, overrides: dict, out_dir: Path,
              tags: list[str] | None = None) -> Path:
    from ..agent.runner import Agent
    from ..config import build_settings
    from ..console import Reporter
    from ..env import MacEnv
    from ..providers.jev import Jev

    c = Console()
    tasks = select(task_ids, tags)
    server = FixtureServer().start()
    env = MacEnv()
    rows: list[dict[str, Any]] = []
    awake = KeepAwake().__enter__()
    record_preexisting()
    os.environ.setdefault("PILOT_BROWSER", "Safari")  # web fixtures open in Safari, never in the user's browser
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"pilotbench-{datetime.now():%Y%m%d-%H%M%S}.json"
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
                        rows.append({"invalid": True, "preset": preset, "task": task.id, "tags": task.tags, "run": n + 1, "pass": False,
                                     "detail": f"setup failed: {type(e).__name__}: {str(e)[:160]}", "claimed": None, "seconds": 0,
                                     "model_calls": 0, "model_seconds": 0, "actions": 0, "escalations": 0, "cost": None, "report": None})
                        c.print(f"[yellow]INVALID[/] setup failed: {type(e).__name__}: {str(e)[:120]}")
                        save(rows)
                        continue
                    time.sleep(0.4)
                    out = Agent(settings, env, Reporter(), Jev()).run(task.render(ctx))
                    ctx.answer = " ".join(x for x in (out.answer, out.summary, out.reason) if x)
                    ctx.claimed = out.success
                    try:
                        ok, detail = task.check(ctx)
                    except Exception as e:
                        ok, detail = False, f"grader error: {e}"
                    try:
                        task.cleanup(ctx)
                    except Exception:
                        pass
                    invalid = bool(INFRA_FAILURE.match(out.reason or ""))
                    rows.append({
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
