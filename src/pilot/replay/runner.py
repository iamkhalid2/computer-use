"""Replay recorded decisions against a model and grade the answers by meaning.

Each frame is rebuilt as one stateless request: the step brief, the actions so far and
their results, harness notes, the screenshot and the element list (or a note that the
list was never recorded). No screen, no VM: the Mac stays yours while it runs, and
each item costs exactly one model call.

Use it to compare models (`--executor`), prompt or format changes (`--system`), or
to check that a harness change didn't make a known failure come back.
"""

from __future__ import annotations

import io
import json
import random
import statistics
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from ..agent import prompts
from .record import history_text  # noqa: F401  (re-exported for callers)
from ..config import ModelSpec, Settings
from ..providers import get_provider
from ..tools import build_specs
from ..types import ImagePart, TextPart
from .grader import grade

NO_LIST = ("(The element list for this screen was not recorded. Act on what the screenshot shows: "
           "`ui` works with a `target` label but not with ids.)")


def render_observation(frame: dict[str, Any], variant: dict[str, Any] | None) -> str:
    """The observation text the model sees, re-rendered for a profile variant.

    Recorded frames keep an element snapshot, so the element list can be rebuilt at a
    different length, or dropped entirely (screenshots only), without a new run."""
    original = frame.get("obs_text")
    v = variant or {}
    if v.get("use_ax") is False:
        header = original.split("\nUI elements")[0] if original else ""
        return (header + "\n" if header else "") + "(No element list: act on what the screenshot shows with the computer tool.)"
    if not original:
        return NO_LIST
    header = original.split("\nUI elements")[0]
    lines, chars = v.get("outline_max_lines"), v.get("outline_max_chars")
    if not (lines or chars) or not frame.get("elements"):
        return original
    from ..mac.ax import Node, Snapshot
    from .. import outline as ol

    nodes = []
    for e in frame["elements"]:
        b = e.get("box")
        nodes.append(Node(e.get("role") or "AXUnknown", e.get("subrole"), e.get("label") or "", e.get("value"),
                          tuple(b) if b else None, e.get("enabled", True), id=e.get("id", 0)))
    body = ol.outline(Snapshot("", 0, None, "", nodes), lambda x, y: (round(x), round(y)), lines or 180, chars or 8000)
    return f"{header}\nUI elements (use ids with the `ui` tool; @x,y = screenshot coords):\n{body}"


def build_input(frame: dict[str, Any], root: Path, variant: dict[str, Any] | None = None) -> list[Any]:
    text = frame.get("brief") or f"Task: {frame.get('task')}"
    if frame.get("history"):
        text += "\n\nActions so far in this step (most recent last):\n" + history_text(frame)
    for note in frame.get("notes") or []:
        text += f"\n{note}"
    text += "\n\nCurrent screen:"
    path = root / frame["image"]
    media = "image/png" if path.suffix == ".png" else "image/jpeg"
    w, h = frame.get("screen") or (0, 0)
    if (variant or {}).get("blind"):  # negative control: nothing to see, nothing listed
        buf = io.BytesIO()
        Image.new("RGB", (max(w, 1), max(h, 1)), (128, 128, 128)).save(buf, "PNG")
        return [TextPart(text), ImagePart(buf.getvalue(), "image/png", w, h), TextPart(render_observation(frame, {"use_ax": False}))]
    return [TextPart(text), ImagePart(path.read_bytes(), media, w, h), TextPart(render_observation(frame, variant))]


def select(frames: list[dict[str, Any]], kinds: list[str], tags: list[str] | None, limit: int | None, sample_seed: int) -> list[dict[str, Any]]:
    chosen = [f for f in frames if f["label"]["kind"] in kinds and (not tags or set(tags) & set(f["label"].get("tags", [])))]
    hard = [f for f in chosen if f["label"]["kind"] == "hard"]
    soft = [f for f in chosen if f["label"]["kind"] != "hard"]
    if limit is not None and len(hard) + len(soft) > limit:
        # Hard items first; soft items sampled across tasks so no single task dominates.
        rng = random.Random(sample_seed)
        by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for f in soft:
            by_task[(f.get("outcome") or {}).get("task_id") or "?"].append(f)
        for v in by_task.values():
            rng.shuffle(v)
        picked: list[dict[str, Any]] = []
        while len(picked) < max(0, limit - len(hard)) and any(by_task.values()):
            for k in sorted(by_task):
                if by_task[k] and len(picked) < limit - len(hard):
                    picked.append(by_task[k].pop())
        soft = picked
        hard = hard[:limit]
    return hard + soft


def run_one(frame: dict[str, Any], spec: ModelSpec, system: str, root: Path, variant: dict[str, Any] | None = None) -> dict[str, Any]:
    provider = get_provider(spec)
    space = "pixels" if provider.native_computer else spec.coord_space
    settings = Settings(planner=None, executor=spec, interactive=False)
    ax = (variant or {}).get("use_ax") is not False
    tools = build_specs(settings, space, tuple(frame.get("screen") or (1470, 956)), planner_mode=frame.get("planner_mode", True), ax=ax)
    session = provider.session(system, tools)
    t0 = time.monotonic()
    try:
        turn = session.send(build_input(frame, root, variant))
    finally:
        if hasattr(session, "close"):
            session.close()
    calls = [{"name": c.name, "args": c.args} for c in turn.calls]
    g = grade(frame, calls, cand_space=space)
    return {"id": frame["id"], "kind": frame["label"]["kind"], "tags": frame["label"].get("tags", []),
            "task": (frame.get("outcome") or {}).get("task_id"), "pass": g["pass"], "why": g["why"], "calls": calls,
            "text": turn.text[:300], "latency": round(time.monotonic() - t0, 2), "actions": len(calls),
            "tokens_in": turn.usage.input_tokens + turn.usage.cache_read + turn.usage.cache_write, "tokens_out": turn.usage.output_tokens}


def replay(frames: list[dict[str, Any]], spec: ModelSpec, root: Path, *, system: str | None = None, parallel: int = 2,
           on_result: Any = None, variant: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    system = system or prompts.EXECUTOR
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = {pool.submit(run_one, f, spec, system, root, variant): f for f in frames}
        for fut in as_completed(futures):
            f = futures[fut]
            try:
                r = fut.result()
            except Exception as e:  # usage limit, provider outage: stop early, keep what we have
                r = {"id": f["id"], "kind": f["label"]["kind"], "tags": f["label"].get("tags", []), "error": f"{type(e).__name__}: {e}"}
                if "usage limit" in str(e).lower():
                    for other in futures:
                        other.cancel()
            results.append(r)
            if on_result:
                on_result(r)
    return results


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    done = [r for r in results if "error" not in r]
    graded = [r for r in done if r["pass"] is not None]  # ungradable items are reported, not scored
    hard = [r for r in graded if r["kind"] == "hard"]
    soft = [r for r in graded if r["kind"] != "hard"]
    tags = Counter(t for r in hard for t in r["tags"])
    return {
        "graded": len(graded), "errors": len(results) - len(done), "ungradable": len(done) - len(graded),
        "hard": f"{sum(r['pass'] for r in hard)}/{len(hard)}",
        "hard_by_tag": {t: f"{sum(r['pass'] for r in hard if t in r['tags'])}/{n}" for t, n in tags.items()},
        "soft_agreement": f"{sum(r['pass'] for r in soft)}/{len(soft)}",
        "median_latency": statistics.median(r["latency"] for r in done) if done else None,
        "mean_tokens_in": round(statistics.mean(r["tokens_in"] for r in done)) if done else None,
        "failures": [{"id": r["id"], "kind": r["kind"], "why": r["why"]} for r in graded if not r["pass"]],
    }


def save(results: list[dict[str, Any]], out_dir: Path, meta: dict[str, Any]) -> Path:
    path = out_dir / f"replay-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"meta": meta, "summary": summarise(results), "results": results}, indent=1, default=str))
    return path
