"""Build the replay dataset from run traces.

Two sources:
  recorded  `decisions.jsonl` frames written by DecisionRecorder (full input: element
            list and element snapshot included)
  legacy    frames reconstructed from older `events.jsonl` traces: screenshot, brief and
            action history are exact; the element list was never saved, so these replay
            screenshot-only, and targets are recovered from action results
            ('press [46] popup "Country"', 'left_click on menuitem "Kenya"')

Every frame is tied to its run's graded outcome from PilotBench results, which gives
"soft" labels for free: in a run the grader passed, the action taken is a known-good
choice. Hard labels (curated expect / forbid rules) come from labels.json.
Images are hard-linked, not copied, so the dataset costs no extra disk.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

TARGET_ID = re.compile(r"\[(\d+)\] ([a-z]+)(?: \"([^\"]*)\")?")
TARGET_ON = re.compile(r"\bon ([a-z]+)(?: \"([^\"]*)\")?")


def parse_target(result: str) -> dict[str, str] | None:
    """Recover what an action hit from Pilot's own result text."""
    m = TARGET_ID.search(result or "")
    if m:
        return {"role": m.group(2), "label": m.group(3) or ""}
    m = TARGET_ON.search(result or "")
    if m:
        return {"role": m.group(1), "label": m.group(2) or ""}
    return None


def outcomes(evals_dir: Path) -> dict[str, dict[str, Any]]:
    """run directory name -> graded outcome, from every PilotBench results file."""
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(evals_dir.glob("pilotbench-*.json")):
        try:
            rows = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for r in rows:
            run = Path(r.get("report", "")).parent.name
            if run:
                out[run] = {"pass": bool(r.get("pass")), "invalid": bool(r.get("invalid")), "task_id": r.get("task"),
                            "preset": r.get("preset"), "detail": r.get("detail", "")}
    return out


def legacy_frames(run_dir: Path) -> list[dict[str, Any]]:
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines() if line.strip()]
    if not events or events[0].get("kind") != "start":
        return []
    start = events[0]
    task = start.get("task", "")
    space = start.get("coords", "pixels")
    geo = start.get("geometry") or {}
    screen = [geo.get("model_w", 0), geo.get("model_h", 0)]
    frames: list[dict[str, Any]] = []
    image: str | None = None
    goal = task
    history: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    expected = 0
    n = 0
    for e in events:
        kind = e.get("kind")
        if kind == "observe" and e.get("image"):
            image = e["image"]
        elif kind == "step":
            goal, history = e.get("goal") or task, []
        elif kind == "model" and e.get("role") == "executor":
            if pending is not None:
                frames.append(pending)
            n += 1
            calls = e.get("calls") or []
            brief = f"Task: {task}" + (f"\nCurrent step: {goal}" if goal and goal != task else "")
            pending = {"id": f"{run_dir.name}#{n}", "kind": "decision", "source": "legacy", "task": task, "goal": goal,
                       "brief": brief, "history": history[-12:], "notes": [], "image": image, "obs_text": None,
                       "elements": None, "space": space, "screen": screen, "executor": start.get("executor"),
                       "output": {"calls": calls, "text": e.get("text", ""), "latency": e.get("latency")}, "executed": []}
            expected = len(calls)
        elif kind == "action" and pending is not None and len(pending["executed"]) < expected:
            item = {"name": e["name"], "args": e.get("args") or {}, "ok": bool(e.get("ok")), "result": str(e.get("result", ""))[:400]}
            if (t := parse_target(item["result"])) is not None:
                item["target"] = t
            pending["executed"].append(item)
            history.append(item)
            if e.get("image"):
                image = e["image"]
    if pending is not None:
        frames.append(pending)
    return [f for f in frames if f["image"]]


def recorded_frames(run_dir: Path) -> list[dict[str, Any]]:
    out = []
    for line in (run_dir / "decisions.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        f = json.loads(line)
        f["source"] = "recorded"
        for item in f.get("executed") or []:
            if (t := parse_target(item.get("result", ""))) is not None:
                item["target"] = t
        out.append(f)
    return out


def build(runs_dir: Path, evals_dir: Path, out_dir: Path) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir = out_dir / "images"
    img_dir.mkdir(exist_ok=True)
    graded = outcomes(evals_dir)
    labels = load_labels(out_dir / "labels.json")
    stats = {"frames": 0, "recorded": 0, "legacy": 0, "soft": 0, "hard": 0, "unlabeled": 0, "skipped_invalid": 0}
    with (out_dir / "frames.jsonl").open("w") as fh:
        for run in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
            meta = graded.get(run.name, {})
            if meta.get("invalid"):
                stats["skipped_invalid"] += 1
                continue
            if (run / "decisions.jsonl").exists():
                frames = recorded_frames(run)
            elif (run / "events.jsonl").exists():
                frames = legacy_frames(run)
            else:
                continue
            for f in frames:
                src = run / f["image"] if f.get("image") else None
                if src is None or not src.exists():
                    continue
                dst = img_dir / f"{run.name}__{f['image']}"
                if not dst.exists():
                    try:
                        os.link(src, dst)  # same bytes, no extra disk
                    except OSError:
                        dst.write_bytes(src.read_bytes())
                f["image"] = f"images/{dst.name}"
                f["outcome"] = meta or None
                f["label"] = labels.get(f["id"]) or ({"kind": "soft"} if meta.get("pass") and f.get("executed") else {"kind": "unlabeled"})
                stats[f["label"]["kind"]] = stats.get(f["label"]["kind"], 0) + 1
                stats[f["source"]] += 1
                stats["frames"] += 1
                fh.write(json.dumps(f, default=str) + "\n")
    return stats


def load_labels(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {item["frame"]: {"kind": "hard", **{k: v for k, v in item.items() if k != "frame"}} for item in data}


def load(out_dir: Path) -> list[dict[str, Any]]:
    path = out_dir / "frames.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
