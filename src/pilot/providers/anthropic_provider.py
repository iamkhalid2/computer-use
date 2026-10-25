"""Claude through the Anthropic SDK, with the native computer toolset.

Claude models are trained on the computer toolset (`computer_toolset_20260801`),
so pixel actions use it natively; Pilot's semantic tools (ui, find, menu, ...)
sit alongside as ordinary client tools. The history is append-only: assistant
content goes back verbatim, which keeps thinking blocks valid and caches warm.
"""

from __future__ import annotations

import time
from typing import Any

import anthropic

from ..config import ModelSpec
from ..types import AssistantTurn, ImagePart, Part, ToolCall, ToolResult, ToolSpec, Usage
from .base import Provider, Session, parse_json_text

# thinking-off form per model (None = cannot be disabled; use low effort instead)
CAPS: dict[str, dict[str, Any]] = {
    "claude-fable-5-1": {"off": None, "updates": True, "fallbacks": True},
    "claude-opus-5-5": {"off": None, "updates": True, "fallbacks": True},
    "claude-sonnet-5-5": {"off": "between_tools", "updates": True, "fallbacks": True},
    "claude-haiku-5-5": {"off": "disabled", "updates": False, "fallbacks": False},
    "claude-opus-5": {"off": "disabled", "updates": False, "fallbacks": True},
    "claude-sonnet-5": {"off": "disabled", "updates": False, "fallbacks": False},
}
DEFAULT_CAPS = {"off": "disabled", "updates": False, "fallbacks": False}

BETA_CONTEXT = "context-management-2025-06-27"
BETA_UPDATES = "thinking-display-updates-2026-08-18"
BETA_FALLBACK = "server-side-fallback-2026-07-01"

CONTEXT_EDITS = {
    "edits": [
        {
            "type": "clear_tool_uses_20250919",
            "trigger": {"type": "input_tokens", "value": 60000},
            "keep": {"type": "tool_uses", "value": 12},
            "clear_at_least": {"type": "input_tokens", "value": 20000},
        }
    ]
}


def _blocks(parts: list[Part]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in parts:
        if isinstance(p, ImagePart):
            out.append({"type": "image", "source": {"type": "base64", "media_type": p.media_type, "data": p.b64}})
        else:
            out.append({"type": "text", "text": p.text})
    return out


def _dump(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    return dict(obj or {})


class AnthropicProvider(Provider):
    native_computer = True

    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self.caps = CAPS.get(spec.model, DEFAULT_CAPS)
        self.client = anthropic.Anthropic(max_retries=3, timeout=180.0)
        self.disabled: set[str] = set()  # features the API rejected; dropped for the rest of the run

    # -- request building ---------------------------------------------------------------
    def thinking(self) -> dict[str, Any]:
        if self.spec.thinking == "off" and self.caps["off"]:
            return {"type": self.caps["off"]}
        t: dict[str, Any] = {"type": "adaptive"}
        if self.caps["updates"] and "updates" not in self.disabled:
            t["display"] = "updates"
        return t

    def effort(self) -> str:
        e = self.spec.effort or "medium"
        # thinking-off forms only accept effort high or below
        if self.spec.thinking == "off" and e in {"xhigh", "max"}:
            return "high"
        return e

    def common(self) -> tuple[dict[str, Any], list[str]]:
        kw: dict[str, Any] = {"model": self.spec.model, "output_config": {"effort": self.effort()}}
        betas: list[str] = []
        th = self.thinking()
        kw["thinking"] = th
        if th.get("display") == "updates":
            betas.append(BETA_UPDATES)
        if self.caps["fallbacks"] and "fallbacks" not in self.disabled:
            kw["fallbacks"] = "default"
            betas.append(BETA_FALLBACK)
        return kw, betas

    def call(self, **kw: Any) -> Any:
        """messages.create with graceful feature downgrade on 400s."""
        for _ in range(4):
            try:
                return self.client.beta.messages.create(**kw)
            except anthropic.BadRequestError as e:
                msg = str(e).lower()
                dropped = False
                for feature, key in (("fallbacks", "fallback"), ("updates", "display"), ("context", "context_management")):
                    if key in msg and feature not in self.disabled:
                        self.disabled.add(feature)
                        dropped = True
                if not dropped:
                    raise
                kw = self._strip(kw)
        return self.client.beta.messages.create(**kw)

    def _strip(self, kw: dict[str, Any]) -> dict[str, Any]:
        kw = dict(kw)
        betas = list(kw.get("betas", []))
        if "fallbacks" in self.disabled:
            kw.pop("fallbacks", None)
            betas = [b for b in betas if b != BETA_FALLBACK]
        if "updates" in self.disabled and isinstance(kw.get("thinking"), dict):
            kw["thinking"] = {k: v for k, v in kw["thinking"].items() if k != "display"}
            betas = [b for b in betas if b != BETA_UPDATES]
        if "context" in self.disabled:
            kw.pop("context_management", None)
            betas = [b for b in betas if b != BETA_CONTEXT]
        kw["betas"] = betas
        return kw

    def session(self, system: str, tools: list[ToolSpec]) -> "AnthropicSession":
        return AnthropicSession(self, system, tools)

    def complete_json(self, system: str, parts: list[Part], schema: dict[str, Any]) -> tuple[dict[str, Any], AssistantTurn]:
        kw, betas = self.common()
        kw["output_config"] = {**kw["output_config"], "format": {"type": "json_schema", "schema": schema}}
        t0 = time.monotonic()
        resp = self.call(
            **kw, betas=betas, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": _blocks(parts)}],
        )
        turn = to_turn(resp, time.monotonic() - t0)
        if turn.stop == "refusal":
            raise RuntimeError(f"model refused: {turn.detail}")
        return parse_json_text(turn.text), turn


def to_turn(resp: Any, latency: float) -> AssistantTurn:
    turn = AssistantTurn(latency=latency, model=getattr(resp, "model", ""))
    texts: list[str] = []
    for block in resp.content:
        btype = getattr(block, "type", "")
        if btype == "text":
            texts.append(block.text)
        elif btype == "thinking":
            note = (getattr(block, "thinking", "") or "").strip()
            if note:
                turn.notes.append(note)
        elif btype == "tool_use":
            toolset = getattr(block, "toolset_name", None)
            args = _dump(block.input)
            if toolset == "computer":
                turn.calls.append(ToolCall(block.id, "computer", {"action": block.name, **args}, native=True))
            else:
                turn.calls.append(ToolCall(block.id, block.name, args))
    turn.text = "\n".join(t for t in texts if t.strip())
    u = resp.usage
    turn.usage = Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )
    stop = resp.stop_reason
    if stop == "refusal":
        turn.stop = "refusal"
        details = getattr(resp, "stop_details", None)
        turn.detail = getattr(details, "explanation", "") or getattr(details, "category", "") or "refused"
    elif turn.calls:
        turn.stop = "max_tokens" if stop == "max_tokens" else "tool_use"
    elif stop == "max_tokens":
        turn.stop = "max_tokens"
    else:
        turn.stop = "end"
    return turn


class AnthropicSession(Session):
    def __init__(self, provider: AnthropicProvider, system: str, tools: list[ToolSpec]):
        self.p = provider
        self.system = [{"type": "text", "text": system}]
        self.tools: list[dict[str, Any]] = []
        if any(t.name == "computer" for t in tools):
            self.tools.append({"type": "computer_toolset_20260801"})
        for t in tools:
            if t.name == "computer":
                continue
            self.tools.append({"name": t.name, "description": t.description, "input_schema": t.schema})
        self.messages: list[dict[str, Any]] = []

    def _request(self) -> AssistantTurn:
        kw, betas = self.p.common()
        if "context" not in self.p.disabled:
            kw["context_management"] = CONTEXT_EDITS
            betas.append(BETA_CONTEXT)
        t0 = time.monotonic()
        resp = self.p.call(
            **kw, betas=betas, max_tokens=16000, system=self.system, tools=self.tools,
            messages=self.messages, cache_control={"type": "ephemeral"},
        )
        turn = to_turn(resp, time.monotonic() - t0)
        # Append verbatim: thinking blocks and fallback markers must round-trip unchanged.
        self.messages.append({"role": "assistant", "content": resp.content})
        if self.on_note:
            for n in turn.notes:
                self.on_note(n)
        return turn

    def send(self, parts: list[Part]) -> AssistantTurn:
        self.messages.append({"role": "user", "content": _blocks(parts)})
        return self._request()

    def reply(self, calls: list[ToolCall], results: list[ToolResult], extra: list[Part] | None = None) -> AssistantTurn:
        by_id = {r.call_id: r for r in results}
        content: list[dict[str, Any]] = []
        for c in calls:
            r = by_id.get(c.id) or ToolResult(c.id, False, "Not executed.")
            body: list[dict[str, Any]] = []
            if r.image is not None:
                body.append(_blocks([r.image])[0])
            text = r.text if not r.observation else f"{r.text}\n{r.observation}"
            if text:
                body.append({"type": "text", "text": text})
            block: dict[str, Any] = {"type": "tool_result", "tool_use_id": c.id, "content": body or [{"type": "text", "text": "OK"}]}
            if not r.ok:
                block["is_error"] = True
            if c.native:
                block["toolset_name"] = "computer"
            content.append(block)
        content.extend(_blocks(extra or []))
        self.messages.append({"role": "user", "content": content})
        return self._request()
