"""`pilot tune mine`: where the turns go, read from the raw traces.

Once every task passes, failures are too rare to steer by; the signal is waste. Each
executor turn and action in `runs/*/events.jsonl` is classified by a signature, and
signatures are clustered with what they cost (turns, model seconds, tokens) and a few
raw examples. No model calls.

  refused:<why>       a safety refusal, then a retry by another route
  error:<tool>:<msg>  a tool call that failed (messages normalised: quotes and numbers dropped)
  look-only           a turn that only looked (screenshot, zoom, wait): one arrives after every
                      acting turn anyway, so each of these is a full model round trip for nothing new
  repeat              the same calls as the turn before
  escalate:<why>      the executor gave up on a step and the planner had to rescue it
  wait-timeout        waiting for an editable field that never took focus
  context-growth      a run whose executor input grew past 30k tokens per call (cost and latency
                      grow with every turn the provider keeps)

The sealed split lives here too, so mining and proposing can only ever see `dev` tasks.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOOK = {"screenshot", "zoom", "wait"}
SALT = "pilot-split-v1"  # changing it re-deals the split: only do that with a new task suite


# -- the sealed split -----------------------------------------------------------------------------
def split(tasks: list[Any]) -> dict[str, str]:
    """task id -> "dev" | "test". Red-team tasks are a safety gate, never an optimisation target: "gate".

    Stratified by each task's first axis, so the sealed tasks cover speed, long, intelligence and
    precision; within an axis, a salted hash decides, so the split is stable and nobody picks it."""
    out: dict[str, str] = {}
    by_axis: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        if "redteam" in t.tags:
            out[t.id] = "gate"
        else:
            by_axis[t.tags[0] if t.tags else "-"].append(t.id)
    for ids in by_axis.values():
        ranked = sorted(ids, key=lambda i: hashlib.sha256(f"{SALT}:{i}".encode()).hexdigest())
        n_test = max(1, round(len(ranked) / 3)) if len(ranked) > 1 else 0
        for k, i in enumerate(ranked):
            out[i] = "test" if k < n_test else "dev"
    return out


def run_index(evals_dir: Path, runs_dir: Path | None = None) -> tuple[dict[str, dict[str, Any]], list[set[str]]]:
    """run directory name -> its PilotBench row, and the runs of each bench round, newest first.

    Older runs with no row (bench rounds before rows linked their reports, ad-hoc sessions) are
    matched by task text with the fixture port masked (it changes between rounds), so a sealed
    task can't slip into dev under another name."""
    idx: dict[str, dict[str, Any]] = {}
    rounds: list[set[str]] = []
    for f in sorted(evals_dir.glob("pilotbench-*.json"), reverse=True):
        try:
            rows = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        names = {Path(r["report"]).parent.name for r in rows if r.get("report")}
        if names:
            rounds.append(names)
        for r in rows:
            if r.get("report"):
                idx.setdefault(Path(r["report"]).parent.name, r)
    if runs_dir is not None:
        by_text: dict[str, str] = {}
        for run, r in idx.items():
            start = _start(runs_dir / run / "events.jsonl")
            if start:
                by_text[_text_key(start)] = r["task"]
        for f in runs_dir.glob("*/events.jsonl"):
            if f.parent.name not in idx:
                start = _start(f)
                if start and _text_key(start) in by_text:
                    idx[f.parent.name] = {"task": by_text[_text_key(start)], "unlinked": True}
    return idx, rounds


def _start(path: Path) -> str | None:
    try:
        with path.open() as fh:
            first = json.loads(fh.readline() or "{}")
    except (OSError, json.JSONDecodeError):
        return None
    return first.get("task") if first.get("kind") == "start" else None


def _text_key(task: str) -> str:
    """Only the fixture port changes between rounds; other numbers tell tasks apart."""
    return re.sub(r":\d{2,5}\b", ":PORT", task.lower()).strip()


# -- signatures ---------------------------------------------------------------------------------
def norm(msg: str) -> str:
    msg = re.sub(r"'[^']*'|\"[^\"]*\"|“[^”]*”", "…", msg or "")
    msg = re.sub(r"\d+(\.\d+)?", "N", msg)
    return re.sub(r"\s+", " ", msg).strip()[:70]


def _sig(calls: list[dict[str, Any]] | None) -> str:
    return json.dumps(calls or [], sort_keys=True)


@dataclass
class Cluster:
    signature: str
    count: int = 0
    turns: float = 0
    seconds: float = 0.0
    tokens: int = 0
    runs: set[str] = field(default_factory=set)
    tasks: set[str] = field(default_factory=set)
    examples: list[dict[str, Any]] = field(default_factory=list)

    def add(self, run: str, task: str, turns: float, seconds: float, tokens: int, example: dict[str, Any]) -> None:
        self.count += 1
        self.turns += turns
        self.seconds += seconds
        self.tokens += tokens
        self.runs.add(run)
        self.tasks.add(task)
        if len(self.examples) < 3 and run not in {e["run"] for e in self.examples}:
            self.examples.append({"run": run, "task": task, **example})

    def to_dict(self) -> dict[str, Any]:
        return {"signature": self.signature, "count": self.count, "turns_lost": round(self.turns, 1),
                "seconds_lost": round(self.seconds, 1), "tokens": self.tokens, "runs": len(self.runs),
                "tasks": sorted(self.tasks), "examples": self.examples}


def mine_run(events: list[dict[str, Any]], run: str, task: str, clusters: dict[str, Cluster]) -> dict[str, Any]:
    def hit(sig: str, turns: float, seconds: float, tokens: int, **ex: Any) -> None:
        clusters.setdefault(sig, Cluster(sig)).add(run, task, turns, seconds, tokens, ex)

    turn: dict[str, Any] | None = None
    prev_sig = None
    exec_in: list[int] = []
    for e in events:
        k = e["kind"]
        if k == "model" and e.get("role") == "executor":
            turn = e
            tokens = int(e.get("in") or 0)
            exec_in.append(tokens)
            calls = e.get("calls") or []
            names = [(c["name"], (c.get("args") or {}).get("action")) for c in calls]
            note = " · ".join(e.get("notes") or [])[:80]
            if calls and all(n == "computer" and a in LOOK for n, a in names):
                hit("look-only", 1, e.get("latency", 0), tokens, t=e["t"], note=note, calls=[a for _, a in names])
            sig = _sig(calls)
            if calls and sig == prev_sig:
                hit("repeat", 1, e.get("latency", 0), tokens, t=e["t"], note=note, calls=calls[:2])
            prev_sig = sig
        elif k == "action" and not e.get("ok", True):
            res = str(e.get("result", ""))
            lat = (turn or {}).get("latency", 0)
            n_calls = max(1, len((turn or {}).get("calls") or []))
            cut = {"call": {"name": e.get("name"), "args": {a: str(v)[:80] for a, v in (e.get("args") or {}).items()}},
                   "result": res[:160], "t": e["t"]}
            if res.startswith(("Not allowed", "Refused", "Blocked")):
                hit("refused:" + norm(res.split(".")[0]), 1 / n_calls, lat / n_calls, 0, **cut)
            else:
                hit(f"error:{e.get('name')}:{norm(res)}", 1 / n_calls, lat / n_calls, 0, **cut)
        elif k == "step_result" and e.get("outcome") == "escalate":
            hit("escalate:" + norm(e.get("reason", "")), 1, 0, 0, t=e["t"], reason=str(e.get("reason", ""))[:200])
        elif k == "scaffold" and e.get("name") == "await_editable" and not e.get("found"):
            hit("wait-timeout", 0, float(e.get("waited") or 0), 0, t=e["t"])
    if exec_in and max(exec_in) > 30000:
        extra = sum(x - exec_in[0] for x in exec_in)
        hit("context-growth", 0, 0, extra, first=exec_in[0], last=exec_in[-1], calls=len(exec_in))
    return {"executor_calls": len(exec_in), "executor_in": sum(exec_in)}


def mine(runs_dir: Path, evals_dir: Path, tasks: list[Any], scope: str = "dev", rounds: int | None = 3) -> dict[str, Any]:
    """Clusters from the runs in `scope` ("dev", "test", "gate" or "all"), ranked by turns lost.

    Only the newest `rounds` bench rounds count by default (older traces describe an older
    harness), plus ad-hoc sessions from the same period; `rounds=None` reads all history.
    Ad-hoc sessions on tasks outside the suite count as dev."""
    sp = split(tasks)
    idx, by_round = run_index(evals_dir, runs_dir)
    window: set[str] | None = None
    if rounds is not None and by_round:
        window = set().union(*by_round[:rounds])
        oldest = min(window) if window else ""
    clusters: dict[str, Cluster] = {}
    last_seen: dict[str, str] = {}
    seen = {"runs": 0, "skipped_invalid": 0, "skipped_scope": 0, "skipped_old": 0, "executor_calls": 0, "executor_in": 0}
    for f in sorted(runs_dir.glob("*/events.jsonl")):
        run = f.parent.name
        row = idx.get(run)
        if window is not None and run not in window and (row and not row.get("unlinked") or run < oldest):
            seen["skipped_old"] += 1
            continue
        task = row["task"] if row else "adhoc"
        where = sp.get(task, "dev")
        if row and row.get("invalid"):
            seen["skipped_invalid"] += 1
            continue
        if scope != "all" and where != scope:
            seen["skipped_scope"] += 1
            continue
        try:
            events = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            continue
        before = {k: c.count for k, c in clusters.items()}
        got = mine_run(events, run, task, clusters)
        for k, c in clusters.items():
            if c.count != before.get(k, 0):
                last_seen[k] = run[:15]
        seen["runs"] += 1
        seen["executor_calls"] += got["executor_calls"]
        seen["executor_in"] += got["executor_in"]
    ranked = sorted(({**c.to_dict(), "last_seen": last_seen.get(c.signature)} for c in clusters.values()),
                    key=lambda c: (-c["turns_lost"], -c["seconds_lost"], -c["tokens"]))
    return {"scope": scope, "rounds": rounds, "split": sp, **seen, "clusters": ranked}
