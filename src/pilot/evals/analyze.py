"""Turn a PilotBench results file plus its run traces into a diagnosis.

For each preset: where wall time went (planner vs executor latency, actions,
observation), how many turns tasks took, which tools failed most, and the
failing runs with the last thing the agent said. This is what the improvement
loop works from.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def load_trace(report: str) -> list[dict[str, Any]]:
    events = Path(report).parent / "events.jsonl"
    if not events.exists():
        return []
    return [json.loads(line) for line in events.read_text().splitlines() if line.strip()]


def analyze(results_path: Path) -> dict[str, Any]:
    rows = json.loads(results_path.read_text())
    out: dict[str, Any] = {}
    by_preset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_preset[r["preset"]].append(r)
    for preset, rs in by_preset.items():
        role_lat: dict[str, list[float]] = defaultdict(list)
        tool_fail: Counter[str] = Counter()
        tool_use: Counter[str] = Counter()
        turns: list[int] = []
        failures = []
        for r in rs:
            ev = load_trace(r["report"])
            n_exec = 0
            last_text = ""
            for e in ev:
                if e["kind"] == "model":
                    role_lat[e["role"]].append(e["latency"])
                    n_exec += e["role"] == "executor"
                    if e.get("text"):
                        last_text = e["text"]
                elif e["kind"] == "action":
                    name = e["name"] if e["name"] != "computer" else f"computer.{(e.get('args') or {}).get('action')}"
                    tool_use[name] += 1
                    if not e.get("ok"):
                        tool_fail[name] += 1
            turns.append(n_exec)
            if not r["pass"] and not r.get("invalid"):
                fin = next((e for e in reversed(ev) if e["kind"] == "finish"), {})
                failures.append({"task": r["task"], "detail": r["detail"], "reason": fin.get("reason", ""), "last_text": last_text[:200],
                                 "seconds": r["seconds"], "report": r["report"]})
        out[preset] = {
            "latency": {role: {"calls": len(v), "median": round(statistics.median(v), 2), "p90": round(sorted(v)[max(0, math.ceil(0.9 * len(v)) - 1)], 2)}
                        for role, v in role_lat.items() if v},
            "median_executor_turns": statistics.median(turns) if turns else 0,
            "tool_failures": {k: f"{tool_fail[k]}/{tool_use[k]}" for k in sorted(tool_use, key=lambda k: -tool_fail[k]) if tool_fail[k]},
            "tool_use": dict(tool_use.most_common()),
            "failures": failures,
        }
    return out


def main(path: str) -> None:
    print(json.dumps(analyze(Path(path)), indent=2))


if __name__ == "__main__":
    import sys

    main(sys.argv[1])
