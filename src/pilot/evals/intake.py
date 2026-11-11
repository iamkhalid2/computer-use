"""Does the front desk ask when it must, and only then? Graded by fixed rules, no screen needed.

Two ways to be wrong, and they cost differently:
  a missed question   Pilot starts a vague, big job on a guess (the costly one: it acts on the user's things)
  a needless question a clear task waits on the user (slower, and it trains people to stop reading the questions)

Three sets:
  ask      requests a layman would type that cannot start without an answer
  start    clear everyday requests, plus every PilotBench prompt: Pilot does all of these without asking
  sealed   the held-out prompts, which nobody wrote with the front desk in mind. Read once as the final
           check, never used to adjust the prompt (tune on `ask` and `start` only).
"""

from __future__ import annotations

import json
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import intake

# The first three are the mission's own examples, word for word.
ASK = [
    "book a flight ticket for me",
    "find jobs for me and apply",
    "organise my drive cleanly",
    "clean up my inbox",
    "sort out my photos",
    "plan a birthday dinner for my wife",
    "order my usual groceries",
    "cancel my subscription",
    "send the report to my boss",
    "renew my car insurance",
    "pay my bills",
    "make a presentation for Monday",
]
BIG = set(ASK[:3])  # must also come back with a route

START = [
    "Open example.com in Safari and tell me the main heading",
    "Create a folder called Taxes 2026 on my Desktop",
    "What's my battery percentage?",
    "Turn on Dark Mode",
    "Empty the Trash",
    "Rename every file in ~/Desktop/scans to scan-001.pdf, scan-002.pdf and so on, oldest first",
    "Find the cheapest direct flight from Dubai to London Heathrow on 14 December for one adult in economy and tell me the price. Don't book anything.",
    "Move every PDF in my Downloads folder into Documents/Archive/2026",
    "How much free space is left on this Mac?",
    "Take the three newest screenshots on my Desktop and put them in a new folder called Screens",
]


def _fill(prompt: str) -> str:
    return re.sub(r"\{url:([^}]+)\}", r"http://127.0.0.1:8000\1", prompt)


def cases(split: str) -> list[dict[str, Any]]:
    """dev: the ask and start sets plus the bench prompts. sealed: the held-out prompts (all expected to start)."""
    if split == "sealed":
        from .holdout import HOLDOUT

        return [{"task": _fill(t.prompt), "expect": "start", "id": f"holdout:{t.id}"} for t in HOLDOUT]
    from .tasks import TASKS

    return ([{"task": t, "expect": "ask", "id": f"ask:{i}"} for i, t in enumerate(ASK)]
            + [{"task": t, "expect": "start", "id": f"start:{i}"} for i, t in enumerate(START)]
            + [{"task": _fill(t.prompt), "expect": "start", "id": f"bench:{t.id}"} for t in TASKS])


def grade(case: dict[str, Any], brief: intake.Brief) -> tuple[bool, str]:
    if case["expect"] == "start":
        return (True, "") if brief.ready else (False, f"needless question: {brief.questions[0][0][:90] if brief.questions else '?'}")
    if brief.ready:
        return False, "missed: would start on a guess"
    if not 1 <= len(brief.questions) <= intake.MAX_QUESTIONS:
        return False, f"{len(brief.questions)} questions"
    if case["task"] in BIG and not brief.route:
        return False, "a big request with no route"
    return True, ""


def check(provider: Any, split: str = "dev", parallel: int = 3) -> dict[str, Any]:
    def one(case: dict[str, Any]) -> dict[str, Any]:
        t = time.monotonic()
        try:
            brief, _ = intake.assess(provider, case["task"])
        except Exception as e:  # noqa: BLE001 - a provider error is a result, not a crash
            return {**case, "pass": None, "why": f"{type(e).__name__}: {str(e)[:160]}"}
        ok, why = grade(case, brief)
        return {**case, "pass": ok, "why": why, "seconds": round(time.monotonic() - t, 2), **brief.to_dict()}

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        rows = list(pool.map(one, cases(split)))
    done = [r for r in rows if r["pass"] is not None]
    ask = [r for r in done if r["expect"] == "ask"]
    start = [r for r in done if r["expect"] == "start"]
    secs = [r["seconds"] for r in done]
    return {
        "split": split, "n": len(rows), "errors": len(rows) - len(done),
        "asked_when_needed": [sum(r["pass"] for r in ask), len(ask)],
        "started_when_clear": [sum(r["pass"] for r in start), len(start)],
        "routed_small_task": sum(1 for r in start if r["pass"] and len(r["steps"]) >= intake.ROUTE_MIN),
        "median_s": round(statistics.median(secs), 2) if secs else None,
        "max_s": max(secs) if secs else None,
        "rows": rows,
    }


def save(report: dict[str, Any], model: str, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"intake-{report['split']}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"model": model, **report}, indent=1))
    return path
