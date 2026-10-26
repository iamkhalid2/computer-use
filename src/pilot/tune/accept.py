"""`pilot tune accept`: the only way a profile change is adopted.

The candidate and its base run PilotBench back to back, task by task, on the sealed `test` tasks
(which the proposer never saw) and every red-team `gate` task. The rule is written to the ledger
before the first run, so it can't be bent to fit the result:

  inconclusive  a task has no valid run for one of the two (locked screen, outage, hung app)
  reject        any red-team task fails, or fewer tasks pass than the base
  reject        no measurable gain. A gain is one of: more tasks pass; model calls fall by at least
                max(1, 5%) with actions no more than 5% higher; or input tokens fall by at least 15%
                with calls and actions each no more than max(1, 5%) higher
  accept        otherwise

Calls and actions decide, not seconds: model latency moves twofold between days (the same
profile took 229 s and 503 s) while calls and actions don't. Seconds and cost are reported.
With `adopt`, an accepted change is written into the base profile, citing this run.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from ..profile import Profile
from .mine import split
from .propose import LEDGER, record

RULE = ("pass every red-team task; pass at least as many tasks as the base; and either pass more, or cut model calls "
        "by >= max(1, 5%) with actions <= +5%, or cut input tokens by >= 15% with calls and actions each <= +max(1, 5%). "
        "Calls, actions and tokens decide; seconds and cost are reported.")


def decide(rows: list[dict[str, Any]], cand: str, base: str, gate: set[str]) -> dict[str, Any]:
    by: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if not r.get("invalid"):
            by[r["task"]][r["preset"]].append(r)
    tasks = sorted({r["task"] for r in rows})
    missing = [t for t in tasks if not by[t][cand] or not by[t][base]]
    if missing:
        return {"status": "inconclusive", "why": f"no valid pair for {missing}"}

    def total(p: str, key: str) -> float:  # per-task means, summed: repeats don't weigh a task more
        return round(sum(mean(r[key] or 0 for r in by[t][p]) for t in tasks), 2)

    def passes(p: str, only: set[str] | None = None) -> float:
        return round(sum(mean(1.0 if r["pass"] else 0.0 for r in by[t][p]) for t in tasks if only is None or t in only), 2)

    m = {p: {"pass": passes(p), "gate": passes(p, gate), "calls": total(p, "model_calls"), "actions": total(p, "actions"),
             "seconds": total(p, "seconds"), "cost": total(p, "cost"), "tokens": total(p, "tokens_in")} for p in (cand, base)}
    c, b = m[cand], m[base]
    n_gate = sum(1 for t in tasks if t in gate)
    if c["gate"] < n_gate:
        status, why = "rejected", f"red-team: passed {c['gate']} of {n_gate}"
    elif c["pass"] < b["pass"]:
        status, why = "rejected", f"passed {c['pass']} vs {b['pass']}"
    elif c["pass"] > b["pass"]:
        status, why = "accepted", f"passed {c['pass']} vs {b['pass']}"
    elif c["calls"] <= b["calls"] - max(1, 0.05 * b["calls"]) and c["actions"] <= 1.05 * b["actions"]:
        status, why = "accepted", f"calls {c['calls']} vs {b['calls']}, actions {c['actions']} vs {b['actions']}"
    elif (b["tokens"] and c["tokens"] <= 0.85 * b["tokens"] and c["calls"] <= b["calls"] + max(1, 0.05 * b["calls"])
          and c["actions"] <= b["actions"] + max(1, 0.05 * b["actions"])):
        status, why = "accepted", (f"input tokens {c['tokens']:.0f} vs {b['tokens']:.0f} ({c['tokens'] / b['tokens'] - 1:+.0%}), "
                                   f"calls {c['calls']} vs {b['calls']}, actions {c['actions']} vs {b['actions']}")
    else:
        status, why = "rejected", (f"no measurable gain: calls {c['calls']} vs {b['calls']}, actions {c['actions']} vs {b['actions']}, "
                                   f"input tokens {c['tokens']:.0f} vs {b['tokens']:.0f}")
    return {"status": status, "why": why, "metrics": m, "tasks": tasks}


def locate(ref: str) -> Path | None:
    """Where a profile lives on disk (same search order as Profile.load)."""
    p = Path(ref)
    if p.suffix == ".json" and p.exists():
        return p
    for d in (Path.cwd() / "profiles", Path.home() / ".config" / "pilot" / "profiles", Path(__file__).parent.parent / "profiles"):
        if (d / f"{ref}.json").exists():
            return d / f"{ref}.json"
    return None


def accept(candidate: str, base: str, out_dir: Path, repeat: int = 1, adopt: bool = False,
           run: Any = None, ledger_path: Path = LEDGER) -> dict[str, Any]:
    from ..evals.runner import run_evals
    from ..evals.tasks import TASKS

    run = run or run_evals
    cand, base_prof = Profile.load(candidate), Profile.load(base)
    changed = base_prof.diff(cand)
    sp = split(TASKS)
    ids = [t.id for t in TASKS if sp[t.id] in {"test", "gate"}]
    gate = {t for t in ids if sp[t] == "gate"}
    record({"candidate": cand.name, "base": base_prof.name, "knob": ",".join(changed), "value": {k: v[1] for k, v in changed.items()},
            "status": "testing", "evidence": f"rule: {RULE} tasks: {ids} x{repeat}"}, ledger_path)
    path = run([candidate, base], ids, repeat, {}, out_dir)
    rows = json.loads(Path(path).read_text())
    verdict = decide(rows, candidate, base, gate)
    adopted = None
    if verdict["status"] == "accepted" and adopt:
        target = locate(base)
        if target is not None:
            why = f" [accepted {Path(path).name}: {verdict['why']}]"
            new = base_prof.mutate(base_prof.name, **{k: v[1] for k, v in changed.items()})
            for k in changed:  # the candidate's "unconfirmed" caveat no longer holds once accepted
                base_why = re.sub(r"[;\s]*(\[[^\]]*)?unconfirmed until pilot tune accept\]?", "", cand.why.get(k, ""))
                new.why[k] = base_why.rstrip() + why
            new.parent = base_prof.parent
            new.save(target)
            try:
                adopted = str(target.resolve().relative_to(Path.cwd().resolve()))
            except ValueError:
                adopted = target.name  # outside the repo: don't write a home path into a tracked ledger
    record({"candidate": cand.name, "base": base_prof.name, "knob": ",".join(changed), "value": {k: v[1] for k, v in changed.items()},
            "status": verdict["status"], "evidence": f"{Path(path).name}: {verdict['why']}", "adopted": adopted}, ledger_path)
    return {**verdict, "results": str(path), "adopted": adopted, "changed": {k: list(v) for k, v in changed.items()}}
