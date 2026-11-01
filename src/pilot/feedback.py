"""Your verdict on a real task, kept beside its trace: `feedback.json` in the run directory.

PilotBench grades its own tasks; real use has no grader but you. A rating turns a finished run
into evidence the harness learns from without running anything again:
  up     the run's decisions become soft replay items: known-good choices on real screens
  down   `pilot tune mine` reads the run as a failure; when Pilot had claimed success, the
         decision that claimed it becomes a hard replay item (claiming done there was wrong,
         wherever the real mistake happened earlier)
Only you rate a run: the prompt after a task or `pilot feedback`. Nothing on screen, in a page or
in a trace can write one.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

FILE = "feedback.json"
UP, DOWN = "up", "down"
ANSWERS = {"y": UP, "yes": UP, "up": UP, "+": UP, "n": DOWN, "no": DOWN, "down": DOWN, "-": DOWN}


def record(run_dir: Path, rating: str, note: str = "", claimed: bool | None = None, source: str = "prompt") -> dict[str, Any]:
    if rating not in {UP, DOWN}:
        raise ValueError(f"rating must be {UP!r} or {DOWN!r}, not {rating!r}")
    if claimed is None:
        claimed = claimed_success(run_dir)
    fb = {"rating": rating, "note": note.strip(), "claimed": claimed, "source": source,
          "at": datetime.now().isoformat(timespec="seconds")}
    (run_dir / FILE).write_text(json.dumps(fb, indent=1))
    return fb


def read(run_dir: Path) -> dict[str, Any] | None:
    try:
        fb = json.loads((run_dir / FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return fb if fb.get("rating") in {UP, DOWN} else None


def claimed_success(run_dir: Path) -> bool | None:
    try:
        return bool(json.loads((run_dir / "summary.json").read_text()).get("success"))
    except (OSError, json.JSONDecodeError):
        return None


def false_done(fb: dict[str, Any] | None) -> bool:
    """Pilot said it succeeded and you said it didn't: the costliest kind of failure."""
    return bool(fb and fb["rating"] == DOWN and fb.get("claimed"))


def latest_run(runs_dir: Path) -> Path | None:
    runs = sorted(p for p in runs_dir.iterdir() if p.is_dir() and (p / "events.jsonl").exists()) if runs_dir.is_dir() else []
    return runs[-1] if runs else None


def ask(run_dir: Path, claimed: bool, prompt: Callable[[str], str]) -> dict[str, Any] | None:
    """One keystroke after a task; Enter skips. A thumbs-down asks what went wrong (optional)."""
    try:
        answer = prompt("Did it do what you asked? [y/n, Enter to skip] ").strip().lower()
        rating = ANSWERS.get(answer)
        if rating is None:
            return None
        note = prompt("What went wrong? (optional) ") if rating == DOWN else ""
    except (EOFError, KeyboardInterrupt):
        return None
    return record(run_dir, rating, note, claimed)
