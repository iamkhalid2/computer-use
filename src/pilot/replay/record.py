"""Record every executor decision as a self-contained, replayable frame.

A frame is exactly what the model saw when it chose an action: the step brief, the
actions so far and their results, any harness notes, the screenshot and the element
list, plus an element snapshot (role, label, value, box in screenshot coordinates)
that lets a grader judge an action by what it targets rather than by its pixels.
Frames are appended to `decisions.jsonl` in the run's trace directory.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..types import AssistantTurn, ToolCall, ToolResult

HISTORY = 12  # most recent actions kept in a frame


def prompt_hash(text: str) -> str:
    return hashlib.blake2b(text.encode(), digest_size=6).hexdigest()


def element_snapshot(env: Any, space: str) -> list[dict[str, Any]]:
    out = []
    for nid, n in sorted(env.registry.items()):
        box = None
        if n.frame:
            x, y, w, h = n.frame
            x0, y0 = env.to_model(x, y, space)
            x1, y1 = env.to_model(x + w, y + h, space)
            box = [x0, y0, x1 - x0, y1 - y0]
        out.append({"id": nid, "role": n.role, "subrole": n.subrole, "label": n.label, "value": n.value,
                    "box": box, "interactive": n.interactive, "enabled": n.enabled})
    return out


class DecisionRecorder:
    def __init__(self, trace: Any, env: Any, *, space: str, executor: str, system: str, tools: list[str], planner_mode: bool):
        self.trace = trace
        self.env = env
        self.meta = {"space": space, "executor": executor, "prompt": prompt_hash(system), "tools": tools,
                     "planner_mode": planner_mode, "screen": [env.geometry.model_w, env.geometry.model_h]}
        self.path = trace.dir / "decisions.jsonl"
        self.n = 0
        self.brief = ""
        self.goal = ""
        self.history: list[dict[str, Any]] = []
        self.pending: dict[str, Any] | None = None

    def begin_step(self, brief: str, goal: str) -> None:
        self.brief, self.goal, self.history = brief, goal, []

    def before(self, image: str | None, obs_text: str | None, notes: list[str] | None = None) -> None:
        """Snapshot the model's input right before an executor call."""
        self.n += 1
        self.pending = {
            "id": f"{self.trace.dir.name}#{self.n}", "kind": "decision", "task": self.trace.task, "goal": self.goal,
            "brief": self.brief, "history": self.history[-HISTORY:], "notes": notes or [], "image": image,
            "obs_text": obs_text, "elements": element_snapshot(self.env, self.meta["space"]), **self.meta,
        }

    def after(self, turn: AssistantTurn) -> None:
        if self.pending is None:
            return
        self.pending["output"] = {"text": turn.text[:1000], "notes": turn.notes[:3], "stop": turn.stop,
                                  "calls": [{"name": c.name, "args": c.args} for c in turn.calls],
                                  "latency": round(turn.latency, 3), "model": turn.model}

    def results(self, calls: list[ToolCall], results: list[ToolResult]) -> None:
        by_id = {r.call_id: r for r in results}
        done = []
        for c in calls:
            r = by_id.get(c.id)
            item = {"name": c.name, "args": c.args, "ok": bool(r and r.ok), "result": (r.text if r else "not executed")[:400]}
            done.append(item)
            self.history.append(item)
        if self.pending is not None:
            self.pending["executed"] = done
            self.flush()

    def flush(self) -> None:
        if self.pending is None:
            return
        with self.path.open("a") as f:
            f.write(json.dumps(self.pending, default=str) + "\n")
        self.pending = None
