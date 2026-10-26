"""Any OpenAI-compatible chat API: OpenAI, Gemini, OpenRouter, Groq, Ollama, LM Studio, vLLM.

Pixel actions go through a generic `computer` function whose action names
mirror Claude's toolset, so every provider shares one dispatcher. Tool messages
can't carry images in this API, so screenshots follow as a user message; only
the most recent few are sent, older ones become a placeholder.
"""

from __future__ import annotations

import json
import time
from typing import Any

import openai

from ..config import PROVIDER_DEFAULTS, ModelSpec
from ..types import AssistantTurn, ImagePart, Part, ToolCall, ToolResult, ToolSpec, Usage
from .base import Provider, Session, parse_json_text

EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def _content(parts: list[Part]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in parts:
        if isinstance(p, ImagePart):
            out.append({"type": "image_url", "image_url": {"url": f"data:{p.media_type};base64,{p.b64}"}})
        else:
            out.append({"type": "text", "text": p.text})
    return out


class OpenAICompatProvider(Provider):
    honours = frozenset({"keep_images"})  # prunes old screenshots client-side
    native_computer = False

    def __init__(self, spec: ModelSpec, keep_images: int = 3):
        self.spec = spec
        self.keep_images = keep_images
        base = spec.base_url or PROVIDER_DEFAULTS.get(spec.provider, (None, ()))[0]
        # Few SDK retries: with fallbacks configured, switching models beats waiting out a busy one.
        retries = 1 if spec.fallbacks else 3
        self.client = openai.OpenAI(base_url=base, api_key=spec.api_key() or "not-needed", max_retries=retries, timeout=90.0)
        self.disabled: set[str] = set()
        self.model = spec.model
        self.queue = list(spec.fallbacks)
        self.on_switch: Any = None

    def _extra(self) -> dict[str, Any]:
        kw: dict[str, Any] = {}
        if "reasoning_effort" not in self.disabled:
            if self.spec.thinking == "off" and self.spec.provider == "gemini" and not self.spec.effort:
                kw["reasoning_effort"] = "none"
            elif self.spec.effort:
                kw["reasoning_effort"] = EFFORT.get(self.spec.effort, self.spec.effort)
        return kw

    def call(self, **kw: Any) -> Any:
        for _ in range(3 + len(self.queue)):
            kw["model"] = self.model
            try:
                return self.client.chat.completions.create(**kw)
            except (openai.InternalServerError, openai.RateLimitError, openai.APITimeoutError, openai.NotFoundError) as e:
                if not self.queue:
                    raise
                old, self.model = self.model, self.queue.pop(0)
                if self.on_switch:
                    self.on_switch(f"{old} unavailable ({type(e).__name__}); switching to {self.model}")
                continue
            except openai.BadRequestError as e:
                msg = str(e).lower()
                if "reasoning" in msg and "reasoning_effort" in kw:
                    self.disabled.add("reasoning_effort")
                    kw.pop("reasoning_effort")
                    continue
                if "response_format" in kw and ("response_format" in msg or "schema" in msg or "json" in msg):
                    rf = kw["response_format"]
                    if rf.get("type") == "json_schema":
                        kw["response_format"] = {"type": "json_object"}
                    else:
                        kw.pop("response_format")
                    continue
                raise
        return self.client.chat.completions.create(**kw)

    def session(self, system: str, tools: list[ToolSpec]) -> "CompatSession":
        return CompatSession(self, system, tools)

    def complete_json(self, system: str, parts: list[Part], schema: dict[str, Any]) -> tuple[dict[str, Any], AssistantTurn]:
        t0 = time.monotonic()
        resp = self.call(
            messages=[
                {"role": "system", "content": system + "\nRespond with a single JSON object only."},
                {"role": "user", "content": _content(parts)},
            ],
            response_format={"type": "json_schema", "json_schema": {"name": "result", "schema": schema}},
            **self._extra(),
        )
        turn = to_turn(resp, time.monotonic() - t0, self.model)
        return parse_json_text(turn.text), turn


def to_turn(resp: Any, latency: float, model: str) -> AssistantTurn:
    choice = resp.choices[0]
    msg = choice.message
    turn = AssistantTurn(latency=latency, model=getattr(resp, "model", model) or model)
    turn.text = (msg.content or "").strip() if isinstance(msg.content, str) else ""
    reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
    if isinstance(reasoning, str) and reasoning.strip():
        turn.notes.append(reasoning.strip()[:600])
    for tc in msg.tool_calls or []:
        raw = tc.function.arguments or "{}"
        try:
            args = json.loads(raw) if raw.strip() else {}
            if not isinstance(args, dict):
                args = {"__invalid__": raw}
        except json.JSONDecodeError:
            args = {"__invalid__": raw}
        turn.calls.append(ToolCall(tc.id or f"call_{len(turn.calls)}", tc.function.name, args))
    fr = choice.finish_reason
    if fr == "content_filter":
        turn.stop = "refusal"
        turn.detail = "content filter"
    elif turn.calls:
        turn.stop = "tool_use"
    elif fr == "length":
        turn.stop = "max_tokens"
    else:
        turn.stop = "end"
    u = getattr(resp, "usage", None)
    if u is not None:
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        turn.usage = Usage(input_tokens=(u.prompt_tokens or 0) - cached, output_tokens=u.completion_tokens or 0, cache_read=cached)
    return turn


class CompatSession(Session):
    def __init__(self, provider: OpenAICompatProvider, system: str, tools: list[ToolSpec]):
        self.p = provider
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self.tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema}}
            for t in tools
        ]

    def _view(self) -> list[dict[str, Any]]:
        """Messages to send: only the newest `keep_images` screenshots keep their pixels."""
        image_msgs = [i for i, m in enumerate(self.messages) if m["role"] == "user" and isinstance(m["content"], list)
                      and any(c.get("type") == "image_url" for c in m["content"])]
        stale = set(image_msgs[: -self.p.keep_images] if self.p.keep_images else image_msgs)
        if not stale:
            return self.messages
        view = []
        for i, m in enumerate(self.messages):
            if i in stale:
                m = {**m, "content": [c if c.get("type") != "image_url" else {"type": "text", "text": "[older screenshot removed]"} for c in m["content"]]}
            view.append(m)
        return view

    def _request(self) -> AssistantTurn:
        t0 = time.monotonic()
        resp = self.p.call(messages=self._view(), tools=self.tools, tool_choice="auto", **self.p._extra())
        turn = to_turn(resp, time.monotonic() - t0, self.p.model)
        msg = resp.choices[0].message
        stored: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            # model_dump keeps provider extras such as Gemini thought signatures.
            stored["tool_calls"] = [tc.model_dump(exclude_none=True) for tc in msg.tool_calls]
            for i, tc in enumerate(stored["tool_calls"]):
                tc.setdefault("id", turn.calls[i].id)
                tc.setdefault("type", "function")
        self.messages.append(stored)
        if self.on_note:
            for n in turn.notes:
                self.on_note(n)
        return turn

    def send(self, parts: list[Part]) -> AssistantTurn:
        self.messages.append({"role": "user", "content": _content(parts)})
        return self._request()

    def reply(self, calls: list[ToolCall], results: list[ToolResult], extra: list[Part] | None = None) -> AssistantTurn:
        by_id = {r.call_id: r for r in results}
        images: list[Part] = []
        for c in calls:
            r = by_id.get(c.id) or ToolResult(c.id, False, "Not executed.")
            text = ("" if r.ok else "ERROR: ") + r.text
            if r.observation:
                text += "\n" + r.observation
            if r.image is not None:
                text += "\n(screenshot attached below)"
                images.append(r.image)
            self.messages.append({"role": "tool", "tool_call_id": c.id, "content": text})
        follow: list[Part] = list(extra or [])
        if images:
            follow = [*follow, *images[-1:]]
        if follow:
            self.messages.append({"role": "user", "content": _content(follow)})
        return self._request()
