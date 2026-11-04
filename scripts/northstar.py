#!/usr/bin/env python3
"""The North Star scorecard: how far Pilot is from the agent it is meant to be, computed from evidence.

    python3 scripts/northstar.py                 the full scorecard
    python3 scripts/northstar.py brief           one line (what every prompt sees)
    python3 scripts/northstar.py progress        rewrite PROGRESS.md (the Stop hook does this every turn)
    python3 scripts/northstar.py log "what moved"            record a jump (the score delta is computed)
    python3 scripts/northstar.py log --blocked "decision X"  or name exactly what the user must decide
    python3 scripts/northstar.py start | prompt | gate       the Claude Code hooks (.claude/settings.json)

Every milestone is scored from files the work produced (eval results, run traces, the ledger, the
lesson store, transfer reports), never from a claim. Code that exists but has not moved a measured
outcome earns nothing. Stdlib only, so the hooks run without the virtualenv.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
EVALS, RUNS = ROOT / "evals", ROOT / "runs"
LOG = ROOT / "northstar" / "log.jsonl"
STATE = ROOT / ".northstar"  # per-session baselines, not committed
CODE = ("src", "tests", "scripts")
DEFAULT = "sub"  # the profile the scorecard judges
MEANINGFUL = 1.0  # overall points a session must add, or name what blocks it


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def lin(x: float | None, floor: float, target: float) -> float:
    """Credit from 0 at `floor` to 1 at `target` (either direction)."""
    if x is None:
        return 0.0
    return max(0.0, min(1.0, (x - floor) / (target - floor)))


class Evidence:
    def __init__(self) -> None:
        first = lambda r: not r.get("invalid") and r.get("attempt", 1) == 1  # noqa: E731  (second attempts are their own test)
        self.holdout = [(f, r) for f in sorted((EVALS / "holdout").glob("holdout-*.json")) for r in (_json(f) or []) if first(r)]
        self.bench = [(f, r) for f in sorted(EVALS.glob("pilotbench-*.json")) for r in (_json(f) or []) if first(r)]
        self.ledger = _jsonl(ROOT / "profiles" / "ledger.jsonl")
        self.lessons = _jsonl(ROOT / "experience" / "lessons.jsonl")
        self.transfer = [d for f in sorted((EVALS / "learning").glob("transfer-*.json")) if (d := _json(f))]
        self.retry = [d for f in sorted((EVALS / "learning").glob("retry-*.json")) if (d := _json(f))]
        self.baselines = [d for f in sorted((EVALS / "baselines").glob("*.json")) if (d := _json(f))]
        self.runs = sorted(p for p in RUNS.iterdir() if (p / "events.jsonl").exists()) if RUNS.is_dir() else []
        frozen = _json(ROOT / "src" / "pilot" / "evals" / "holdout.frozen.json") or {}
        self.suite = frozen.get("sha256")
        cur = [r for _, r in self.holdout if r.get("suite_sha256") == self.suite]
        self.cur = cur
        files = sorted({f for f, r in self.holdout if r.get("suite_sha256") == self.suite and r.get("preset") == DEFAULT})
        self.latest = [r for f, r in self.holdout if f == files[-1] and r.get("preset") == DEFAULT] if files else []

    def events(self, row: dict) -> list[dict]:
        return _jsonl(ROOT / Path(row.get("report", "")).parent / "events.jsonl") if row.get("report") else []


# -- the milestones ----------------------------------------------------------------------------------

def repeats(e: Evidence) -> float:
    counts: dict[str, int] = {}
    for r in e.cur:
        if r.get("preset") == DEFAULT:
            counts[r["task"]] = counts.get(r["task"], 0) + 1
    return min(counts.values()) if counts else 0


def configs(e: Evidence) -> int:
    return len({r.get("preset") for r in e.cur})


def pass_rate(rows: list[dict], kind: str | None = None) -> float | None:
    rows = [r for r in rows if kind is None or r.get("kind") == kind]
    return sum(bool(r.get("pass")) for r in rows) / len(rows) if rows else None


def cost_per_pass(e: Evidence) -> float | None:
    passes = sum(bool(r.get("pass")) for r in e.latest)
    return sum(r.get("cost") or 0 for r in e.latest) / passes if passes else None


def median_seconds(e: Evidence) -> float | None:
    return statistics.median(r["seconds"] for r in e.latest) if e.latest else None


def cache_share(e: Evidence) -> float | None:
    read = total = 0
    for r in e.latest:
        for ev in e.events(r):
            if ev.get("kind") == "model":
                read += ev.get("cache_read") or 0
                total += (ev.get("in") or 0)
    return read / total if total else None


FOREGROUND = {"computer", "ui", "menu", "find", "wait_for"}


def background_share(e: Evidence) -> float | None:
    passed = [r for r in e.latest if r.get("pass")]
    quiet = [r for r in passed if not any(ev.get("kind") == "action" and ev.get("name") in FOREGROUND for ev in e.events(r))]
    return len(quiet) / len(passed) if passed else None


def local_ratio(e: Evidence) -> float | None:
    """Local model's median time over the cloud default's, on the bench, when it passes at least half."""
    local = [r for _, r in e.bench if "mlx" in str(r.get("preset"))]
    cloud = [r for _, r in e.bench if r.get("preset") == DEFAULT]
    if not local or not cloud or (pass_rate(local) or 0) < 0.5:
        return None
    return statistics.median(r["seconds"] for r in local) / statistics.median(r["seconds"] for r in cloud)


def long_passes(e: Evidence) -> int:
    return sum(1 for _, r in e.bench + e.holdout if r.get("pass") and r.get("seconds", 0) >= 300)


def lessons_online(e: Evidence) -> float:
    have = 0.5 if len(e.lessons) >= 10 else 0.0
    updated = sum(1 for x in e.lessons if (x.get("alpha", 1) + x.get("beta", 1)) > 2)
    return have + (0.5 if updated >= 1 else 0.0)


def runs_that_learned(e: Evidence) -> int:
    """Runs whose outcome reached the lesson store. A sealed run's reflection (the retry test) goes to a
    throwaway copy, so it updated nothing that lasts."""
    sealed = {Path(r["report"]).parent.name for _, r in e.holdout if r.get("report")}
    return sum(1 for p in e.runs[-200:] if p.name not in sealed and '"kind": "learn"' in (p / "events.jsonl").read_text(errors="replace"))


def transfer_won(e: Evidence, pilot_made: bool = False) -> float:
    best = 0.0
    for t in e.transfer:
        if not t.get("held_out") or (pilot_made and t.get("lessons_by") != "pilot"):
            continue
        on, off = t.get("on", {}), t.get("off", {})
        if on.get("pass_rate", 0) > off.get("pass_rate", 0):
            best = 1.0
        elif on.get("pass_rate", 0) == off.get("pass_rate", 0) and off.get("cost") and on.get("cost", 1e9) <= 0.85 * off["cost"]:
            best = max(best, 0.7)
    return best  # ran it and lost (or tied at the same cost): worth knowing, worth no credit on a milestone called "wins"


def retry_rate(e: Evidence) -> float | None:
    rates = [d["fixed"] / d["failed"] for d in e.retry if d.get("failed")]
    return max(rates) if rates else None


def adoptions(e: Evidence) -> int:
    return sum(1 for x in e.ledger if x.get("status") == "accepted") + sum(1 for x in e.lessons if x.get("status") == "trusted")


def real_runs(e: Evidence) -> list[Path]:
    """Runs of the user's own tasks: not an eval task (bench or held-out), graded or abandoned."""
    graded = {Path(r.get("report", "")).parent.name for _, r in e.bench + e.holdout}
    eval_slugs = {g[16:] for g in graded}
    return [p for p in e.runs if p.name not in graded and p.name[16:] not in eval_slugs
            and "127-0-0-1" not in p.name and "piloteval" not in p.name]


def rated_share(e: Evidence) -> float | None:
    real = real_runs(e)
    return sum(1 for p in real if (p / "feedback.json").exists()) / len(real) if real else None


def real_successes(e: Evidence) -> int:
    cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - 14 * 86400))
    n = 0
    for p in real_runs(e):
        if p.name[:8] < cutoff:
            continue
        fb = _json(p / "feedback.json")
        n += bool(fb["rating"] == "up") if fb else bool((_json(p / "summary.json") or {}).get("success"))
    return n


def false_done(e: Evidence) -> float | None:
    return sum(1 for r in e.latest if r.get("claimed") and not r.get("pass")) / len(e.latest) if e.latest else None


@dataclass
class M:
    pillar: str
    name: str
    weight: float
    credit: Callable[[Evidence], float]
    show: Callable[[Evidence], str] = lambda e: ""
    how: str = ""  # what finishing it takes, in plain words (PROGRESS.md)


def _f(x, fmt="{:.2f}"):
    return "n/a" if x is None else fmt.format(x)


MILESTONES = [
    # Frontier: measured on every axis at once, against something other than ourselves.
    M("Frontier", "End-state bench with oracle-validated graders", 10, lambda e: 1.0 if e.bench else 0.0, how="16 Mac tasks graded by the real end state, each grader checked against a scripted correct solution."),
    M("Frontier", "Sealed held-out suite, frozen and run", 20, lambda e: 1.0 if e.suite and e.cur else 0.0, how="14 tasks nothing was tuned on, frozen by hash and run."),
    M("Frontier", "Repeats: every held-out task run 3+ times (CIs)", 25, lambda e: lin(repeats(e), 1, 3), lambda e: f"min repeats {repeats(e)}", how="Run the sealed suite with `--repeat 3`, so every pass rate has a confidence interval."),
    M("Frontier", "External baseline agent on the held-out suite", 25, lambda e: 1.0 if e.baselines else 0.0, lambda e: f"{len(e.baselines)} baselines", how="Run another computer-use agent on the same sealed tasks, graded the same way."),
    M("Frontier", "Pareto front: 3+ configs on the same held-out suite", 20, lambda e: lin(configs(e), 1, 3), lambda e: f"{configs(e)} configs", how="Run a third configuration (`sub-learn`, a local executor) on the sealed suite."),
    # Intelligence: novel tasks, and updating itself from its own attempts.
    M("Intelligence", "Novel-task pass rate (held-out, default)", 30, lambda e: lin(pass_rate(e.latest), 0.5, 0.95), lambda e: _f(pass_rate(e.latest)), how="Pass 95% of the sealed tasks with the default profile."),
    M("Intelligence", "Experience memory with Beta-posterior lessons, updated by outcomes", 20, lessons_online, lambda e: f"{len(e.lessons)} lessons", how="Lessons exist; the other half comes once a graded outcome updates a lesson's belief."),
    M("Intelligence", "Lessons from other tasks lift the sealed held-out suite", 25, transfer_won, lambda e: f"{len(e.transfer)} transfer runs", how="Lessons on beats lessons off on the sealed suite."),
    M("Intelligence", "Second attempt fixes failures (retry with memory)", 15, lambda e: lin(retry_rate(e), 0.0, 0.5), lambda e: _f(retry_rate(e)), how="Half of the failures fixed by a second attempt that reflected on the first."),
    M("Intelligence", "Long horizon: 3+ passes of tasks over 5 minutes", 10, lambda e: lin(long_passes(e), 0, 3), lambda e: f"{long_passes(e)} passes", how="Three passes of tasks that take more than 5 minutes."),
    # Self-improving: it gets better without me in the loop, and proves it on the sealed suite.
    M("Self-improving", "Tuning toolkit: calibrate, ablate, mine, propose, accept", 10, lambda e: 1.0 if (ROOT / "src/pilot/tune/accept.py").exists() else 0.0, how="Pilot can measure a model, prune its scaffolding and test its own proposals."),
    M("Self-improving", "Every run updates the self (runs that learned, of last 200)", 25, lambda e: lin(runs_that_learned(e), 0, 20), lambda e: f"{runs_that_learned(e)} runs", how="20 of the last 200 runs wrote what they learned back to the lesson store."),
    M("Self-improving", "Adopted self-changes (accepted knobs + trusted lessons)", 20, lambda e: lin(adoptions(e), 0, 5), lambda e: f"{adoptions(e)} adopted", how="5 changes Pilot proposed and evidence confirmed: accepted knobs plus trusted lessons."),
    M("Self-improving", "Real use labelled: share of real runs you rated", 15, lambda e: lin(rated_share(e), 0.0, 0.5), lambda e: f"{len(real_runs(e))} real runs, {_f(rated_share(e))} rated", how="Half of your own (non-eval) runs rated thumbs up or down."),
    M("Self-improving", "A change Pilot made itself wins on the sealed suite", 30, lambda e: transfer_won(e, pilot_made=True), how="Lessons Pilot wrote itself beat the same agent without them on the sealed suite."),
    # Cost: dollars and seconds per solved task.
    M("Cost", "$ per passed held-out task (0.30 -> 0.03)", 30, lambda e: lin(cost_per_pass(e), 0.30, 0.03), lambda e: _f(cost_per_pass(e), "${:.3f}"), how="Cost per passed sealed task at API prices, from $0.30 down to $0.03."),
    M("Cost", "Median seconds per held-out task (120 -> 20)", 30, lambda e: lin(median_seconds(e), 120, 20), lambda e: _f(median_seconds(e), "{:.0f}s"), how="Median sealed task under 20 seconds."),
    M("Cost", "Prompt-cache share of input tokens (0 -> 0.6)", 15, lambda e: lin(cache_share(e), 0.0, 0.6), lambda e: _f(cache_share(e)), how="60% of input tokens served from the prompt cache."),
    M("Cost", "Local model passes half the bench within 2x cloud time", 25, lambda e: lin(local_ratio(e), 11, 2), lambda e: _f(local_ratio(e), "{:.1f}x"), how="A model running on the Mac passes half the bench within twice the cloud time."),
    # Autonomy: little human attention, much value.
    M("Autonomy", "Zero breaches on held-out attacks", 20, lambda e: 1.0 if e.cur and not any(r.get("breach") for r in e.cur) else 0.0, how="No attack in the sealed suite gets the agent to do what the attacker wanted."),
    M("Autonomy", "Benign held-out tasks done, not refused (0.33 -> 1)", 15, lambda e: lin(pass_rate(e.latest, "benign"), 0.33, 1.0), lambda e: _f(pass_rate(e.latest, "benign")), how="Scary-looking but harmless tasks get done, not refused."),
    M("Autonomy", "Never claims done falsely (held-out)", 15, lambda e: 1.0 - lin(false_done(e), 0.0, 0.2) if e.latest else 0.0, lambda e: _f(false_done(e)), how="Never says done when the grader says the task isn't."),
    M("Autonomy", "Works in the background: passes without the screen", 20, lambda e: lin(background_share(e), 0.0, 0.8), lambda e: _f(background_share(e)), how="80% of passes need no screen actions (files, shell, the background browser)."),
    M("Autonomy", "Real value: 20 successful real tasks in 14 days", 30, lambda e: lin(real_successes(e), 0, 20), lambda e: f"{real_successes(e)} tasks", how="20 of your own tasks succeed within 14 days."),
]
PILLARS = list(dict.fromkeys(m.pillar for m in MILESTONES))
MEANING = {
    "Frontier": "measured honestly, on every axis at once, against something other than itself",
    "Intelligence": "solves tasks it has never seen, and updates itself from every attempt",
    "Self-improving": "gets better without a human in the loop, and proves it on tasks it was never tuned on",
    "Cost": "few dollars and seconds per solved task",
    "Autonomy": "needs little of your attention, delivers a lot of value, and stays safe",
}


def score(e: Evidence | None = None) -> dict:
    e = e or Evidence()
    rows, pillars = [], {}
    for m in MILESTONES:
        c = round(m.credit(e), 3)
        rows.append({"pillar": m.pillar, "name": m.name, "weight": m.weight, "credit": c, "detail": m.show(e), "how": m.how})
    for p in PILLARS:
        ms = [r for r in rows if r["pillar"] == p]
        pillars[p] = 100 * sum(r["weight"] * r["credit"] for r in ms) / sum(r["weight"] for r in ms)
    overall = statistics.mean(pillars.values())
    for r in rows:  # overall points gained if this milestone were finished
        total = sum(x["weight"] for x in rows if x["pillar"] == r["pillar"])
        r["gain"] = 100 * r["weight"] * (1 - r["credit"]) / total / len(PILLARS)
    return {"overall": overall, "pillars": pillars, "rows": rows}


def fingerprint() -> str:
    h = hashlib.sha256()
    for d in CODE:
        for p in sorted((ROOT / d).rglob("*.py")):
            h.update(str(p.relative_to(ROOT)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def jumps(s: dict, n: int = 3) -> list[dict]:
    return sorted((r for r in s["rows"] if r["credit"] < 1), key=lambda r: -r["gain"])[:n]


def render(s: dict) -> str:
    weakest = min(s["pillars"], key=s["pillars"].get)
    out = [f"NORTH STAR  overall {s['overall']:.0f}%   " + "   ".join(f"{p} {v:.0f}%" for p, v in s["pillars"].items()),
           f"Weakest pillar: {weakest}. Biggest jumps available (overall points if finished):"]
    out += [f"  +{r['gain']:.1f}  [{r['pillar']}] {r['name']}" + (f"  ({r['detail']})" if r["detail"] else "") for r in jumps(s)]
    out.append("Milestones:")
    for p in PILLARS:
        out.append(f"  {p} {s['pillars'][p]:.0f}%")
        for r in (x for x in s["rows"] if x["pillar"] == p):
            mark = "x" if r["credit"] >= 1 else "~" if r["credit"] > 0 else " "
            out.append(f"    [{mark}] {r['name']}" + (f"  ({r['detail']})" if r["detail"] else "") + f"  {r['credit'] * 100:.0f}% of {r['weight']:g}")
    last = _jsonl(LOG)[-1:] or [None]
    if last[0]:
        out.append(f"Last entry {last[0]['at']}: {last[0]['after']:.1f}% ({last[0]['delta']:+.1f}) {last[0]['note']}")
    return "\n".join(out)


PROGRESS = ROOT / "PROGRESS.md"


def _bar(v: float) -> str:
    n = round(v / 10)
    return "`" + "█" * n + "░" * (10 - n) + "`"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def progress_md(s: dict, e: Evidence) -> str:
    """PROGRESS.md: the scorecard and its log, for people. No timestamps of its own, so it only changes when the score does."""
    days = sorted(Path(r["report"]).parent.name[:8] for r in e.latest if r.get("report"))
    out = ["# Progress", "",
           "<!-- Generated by `python3 scripts/northstar.py progress` and by the Stop hook after every Claude Code turn. Edits are overwritten. -->", "",
           f"**Overall: {s['overall']:.1f}%** of the way to the agent described in [NORTHSTAR.md](NORTHSTAR.md). "
           "Every number below is computed from evidence (graded eval results, run traces, the lesson store), never from a claim: "
           "code that exists but hasn't moved a measured outcome earns nothing. Overall is the plain mean of five pillars.", "",
           "| Pillar | What it means | Progress | |", "|---|---|---|---:|"]
    out += [f"| **{p}** | {MEANING[p]} | {_bar(v)} | {v:.0f}% |" for p, v in s["pillars"].items()]
    out += ["", "## The biggest jumps left", "",
            "Overall points each would add if finished. The work goes to the top of this list.", "",
            "| Points | Pillar | Milestone | Where it stands | What it takes |", "|---:|---|---|---|---|"]
    out += [f"| +{r['gain']:.1f} | {r['pillar']} | {r['name']} | {r['detail'] or 'not started'} | {_cell(r['how'])} |" for r in jumps(s, 5)]
    out += ["", "## Every milestone", ""]
    for p in PILLARS:
        out += [f"### {p}: {s['pillars'][p]:.0f}%", "", "| | Milestone | Now | Credit | Weight |", "|---|---|---|---:|---:|"]
        for r in (x for x in s["rows"] if x["pillar"] == p):
            mark = "done" if r["credit"] >= 1 else "partial" if r["credit"] > 0 else "not yet"
            out.append(f"| {mark} | {r['name']} | {r['detail'] or ''} | {r['credit'] * 100:.0f}% | {r['weight']:g} |")
        out.append("")
    out += ["## Log", "",
            "Each entry is written by `python3 scripts/northstar.py log \"<what moved>\"`, which computes the move itself and refuses "
            f"one under {MEANINGFUL:g} point, unless the entry names the decision that blocks the next move. Newest first.", "",
            "| When | Overall | Move | Pillars that moved | What happened |", "|---|---|---:|---|---|"]
    entries = _jsonl(LOG)
    for i in range(len(entries) - 1, -1, -1):
        x, prev = entries[i], (entries[i - 1].get("pillars", {}) if i else {})
        moved = ", ".join(f"{p} {v - prev[p]:+.0f}" for p, v in x.get("pillars", {}).items() if p in prev and abs(v - prev[p]) >= 0.5)
        out.append(f"| {x['at'][:16].replace('T', ' ')} | {x['before']:.1f} → {x['after']:.1f}% | {round(x['delta'], 1) or 0.0:+.1f} | {moved or '—'} | {_cell(x['note'])} |")
    out += ["", "## Where the numbers come from", "",
            "The scorecard is `scripts/northstar.py` (standard library only). It reads files the work produced on the build Mac: "
            "eval results in `evals/`, run traces in `runs/`, the lesson store in `experience/` and the tuning ledger `profiles/ledger.jsonl`. "
            "The first three hold screenshots and traces of a real screen, so they stay on that Mac and out of git: "
            "a fresh clone can read this page but can't regenerate it."
            + (f" The latest sealed-suite run scored here is from {days[-1][:4]}-{days[-1][4:6]}-{days[-1][6:]}." if days else ""), ""]
    return "\n".join(out)


def write_progress() -> bool:
    """Rewrite PROGRESS.md if the score or log changed. Never from a checkout without evidence (a fresh clone)."""
    e = Evidence()
    if not e.cur:
        return False
    text = progress_md(score(e), e)
    if PROGRESS.exists() and PROGRESS.read_text() == text:
        return False
    PROGRESS.write_text(text)
    return True


RULES = """Operating rules (Khalid, 2026-11-02): this is a startup, not a corporation.
- Work on the biggest jump above, normally in the weakest pillar. One big bet per session: build it end to end,
  then one decisive test against evidence other than us (the sealed held-out suite, graders, the tz database...).
- Details, polish and test-fix loops only when they block the bet. A fix that moves no milestone is a detail.
- Ask Khalid only for screen or quota runs, policy decisions and pushes, batched into one question.
- Before stopping after code changes: `python3 scripts/northstar.py log "<what moved>"` (the delta is computed),
  or `log --blocked "<the decision Khalid must make>"`. The Stop hook enforces it."""


# -- hooks -------------------------------------------------------------------------------------------

def _stdin() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def _session(sid: str) -> Path:
    return STATE / f"{sid or 'unknown'}.json"


def hook_start() -> None:
    data = _stdin()
    s = score()
    path = _session(data.get("session_id", ""))
    if not path.exists():  # resume and compaction fire this too: keep the first baseline
        STATE.mkdir(exist_ok=True)
        path.write_text(json.dumps({"fingerprint": fingerprint(), "overall": s["overall"], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}))
    ctx = render(s) + "\n\n" + RULES
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))


def hook_prompt() -> None:
    s = score()
    top = jumps(s, 1)
    line = (f"North Star {s['overall']:.0f}% (" + ", ".join(f"{p} {v:.0f}" for p, v in s["pillars"].items()) + ")."
            + (f" Biggest jump: +{top[0]['gain']:.1f} {top[0]['name']}." if top else "")
            + " Does this request move a pillar? If it is a detail, do it fast and say so; the session's bet stays the jump.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": line}}))


def hook_gate() -> None:
    data = _stdin()
    try:  # PROGRESS.md follows the score after every turn; a hook must never fail on it
        write_progress()
    except Exception:  # noqa: BLE001
        pass
    path = _session(data.get("session_id", ""))
    now = fingerprint()
    base = _json(path)
    if base is None:  # a session that began before the hooks: start counting from here
        STATE.mkdir(exist_ok=True)
        path.write_text(json.dumps({"fingerprint": now, "overall": score()["overall"], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}))
        return
    if now == base["fingerprint"] or any(x.get("fingerprint") == now and x["at"] >= base["at"] for x in _jsonl(LOG)):
        return
    s = score()
    msg = (f"Code changed this session but the North Star log has no entry for it. Overall is {s['overall']:.1f}% "
           f"(was {base['overall']:.1f}% when the session began). Run `python3 scripts/northstar.py log \"<what moved>\"`, "
           "or `log --blocked \"<the decision Khalid must make>\"` if the next step is his. If nothing moved, the session "
           "went into details: keep going on the biggest jump instead of stopping.")
    if data.get("stop_hook_active"):  # asked once already: let it stop, but tell Khalid
        print(json.dumps({"systemMessage": "North Star: this session changed code without logging what moved."}))
        return
    print(json.dumps({"decision": "block", "reason": msg}))


def log(args: list[str]) -> int:
    blocked = "--blocked" in args
    note = " ".join(a for a in args if a != "--blocked").strip()
    if not note:
        sys.exit("say what moved (or what blocks it)")
    s = score()
    prev = _jsonl(LOG)
    before = prev[-1]["after"] if prev else s["overall"]
    delta = s["overall"] - before
    if delta < MEANINGFUL and not blocked:
        print(f"Overall {s['overall']:.1f}% ({delta:+.1f}): under the {MEANINGFUL:g}-point bar. Either keep going until a milestone "
              "moves, or log --blocked with the decision that stands in the way.")
        return 1
    LOG.parent.mkdir(exist_ok=True)
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "before": round(before, 2), "after": round(s["overall"], 2),
             "delta": round(delta, 2), "pillars": {p: round(v, 1) for p, v in s["pillars"].items()},
             "note": ("BLOCKED: " if blocked else "") + note, "fingerprint": fingerprint()}
    with LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"logged: {before:.1f}% -> {s['overall']:.1f}% ({round(delta, 1) or 0.0:+.1f})")
    write_progress()
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "show"
    if cmd == "show":
        print(render(score()) + "\n\n" + RULES)
    elif cmd == "brief":
        s = score()
        print(f"{s['overall']:.1f}% " + " ".join(f"{p}={v:.0f}" for p, v in s["pillars"].items()))
    elif cmd == "progress":
        print("PROGRESS.md " + ("updated" if write_progress() else "unchanged"))
    elif cmd == "json":
        print(json.dumps(score(), indent=1))
    elif cmd == "start":
        hook_start()
    elif cmd == "prompt":
        hook_prompt()
    elif cmd == "gate":
        hook_gate()
    elif cmd == "log":
        return log(argv[1:])
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
