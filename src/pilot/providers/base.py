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
from ..types import AssistantTurn, Part, ToolCall, ToolResult, ToolSpec, Usage

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


class Provider(ABC):
    spec: ModelSpec
    native_computer: bool = False

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
