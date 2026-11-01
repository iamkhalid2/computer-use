"""The provider interface. Each provider owns its native conversation history.

Owning history (instead of converting a neutral transcript every turn) keeps it
append-only and byte-stable: prompt caches stay warm, and providers that bind
reasoning to the exact prefix (Claude 5.5 "preserved thinking", Gemini thought
signatures) keep working.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Callable

from ..config import ModelSpec
from ..types import AssistantTurn, Part, TextPart, ToolCall, ToolResult, ToolSpec, Usage

# USD per million tokens: input, output, cache read, cache write (5 min).
PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}


def cost_of(model: str, usage: Usage) -> float | None:
    if model.startswith("local:"):
        return 0.0  # runs on this Mac: no per-token price
    p = PRICING.get(model)
    if p is None:
        return None
    return (usage.input_tokens * p[0] + usage.output_tokens * p[1] + usage.cache_read * p[2] + usage.cache_write * p[3]) / 1e6


Notify = Callable[[str], None]


class Session(ABC):
    on_note: Notify | None = None

    @abstractmethod
    def send(self, parts: list[Part]) -> AssistantTurn:
        """Send a user message (task, observation)."""

    @abstractmethod
    def reply(self, calls: list[ToolCall], results: list[ToolResult], extra: list[Part] | None = None) -> AssistantTurn:
        """Answer every tool call of the previous turn, plus optional extra content."""


# The JSON action protocol, for models without native tool calls (the subscription CLI, local models):
# each reply is {"note": ..., "actions": [{"tool": name, "args": {...}}, ...]} and Pilot runs the actions.
ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string", "description": "a short phrase about what you are doing, or your final reply"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"tool": {"type": "string"}, "args": {"type": "object"}},
                "required": ["tool", "args"],
            },
        },
    },
    "required": ["note", "actions"],
}


def action_schema(tools: list[ToolSpec]) -> dict[str, Any]:
    schema = json.loads(json.dumps(ACTION_SCHEMA))
    schema["properties"]["actions"]["items"]["properties"]["tool"]["enum"] = [t.name for t in tools]
    return schema


def tools_text(tools: list[ToolSpec]) -> str:
    return "\n".join(f"- {t.name}: {t.description}\n  schema: {json.dumps(t.schema, separators=(',', ':'))}" for t in tools)


class ActionSession(Session):
    """A session that speaks the JSON action protocol: subclasses send parts and return the parsed reply."""

    n = 0  # actions numbered across the session, for call ids

    @abstractmethod
    def _turn(self, parts: list[Part]) -> AssistantTurn: ...

    def _to_turn(self, data: dict[str, Any], turn: AssistantTurn) -> AssistantTurn:
        note = str(data.get("note") or "").strip()
        for a in data.get("actions") or []:
            if not isinstance(a, dict) or not a.get("tool"):
                continue
            self.n += 1
            args = a.get("args") if isinstance(a.get("args"), dict) else {"__invalid__": json.dumps(a.get("args"))}
            turn.calls.append(ToolCall(f"cc{self.n}", str(a["tool"]), args))
        if turn.calls:
            turn.stop = "tool_use"
            if note:
                turn.notes.append(note)
        else:
            turn.stop = "end"
            turn.text = note
        if self.on_note:
            for n in turn.notes:
                self.on_note(n)
        return turn

    def send(self, parts: list[Part]) -> AssistantTurn:
        return self._turn(parts)

    def reply(self, calls: list[ToolCall], results: list[ToolResult], extra: list[Part] | None = None) -> AssistantTurn:
        by_id = {r.call_id: r for r in results}
        lines = ["Results:"]
        images: list[Part] = []
        observations: list[str] = []
        for i, c in enumerate(calls, 1):
            r = by_id.get(c.id) or ToolResult(c.id, False, "Not executed.")
            lines.append(f"{i}. {c.name} {json.dumps(c.args, ensure_ascii=False)[:200]} → {'ok' if r.ok else 'ERROR'}: {r.text}")
            if r.image is not None:
                images = [r.image]
            if r.observation:
                observations = [r.observation]
        parts: list[Part] = [TextPart("\n".join(lines))]
        parts += images
        parts += [TextPart(o) for o in observations]
        parts += list(extra or [])
        return self._turn(parts)


# Knobs that only some providers implement. A provider lists the ones it honours; for the rest the
# knob is inert, and tuning it would measure noise (keep_images: the CLI keeps its own context).
PROVIDER_KNOBS = frozenset({"keep_images"})


class Provider(ABC):
    spec: ModelSpec
    native_computer: bool = False
    honours: frozenset[str] = frozenset()

    @abstractmethod
    def session(self, system: str, tools: list[ToolSpec]) -> Session: ...

    @abstractmethod
    def complete_json(self, system: str, parts: list[Part], schema: dict[str, Any]) -> tuple[dict[str, Any], AssistantTurn]:
        """One-shot structured call (planner, verifier)."""


def parse_json_text(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    return json.loads(text)
