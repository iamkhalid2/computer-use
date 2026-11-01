"""Local vision-language models on Apple silicon through MLX: `mlx:<Hugging Face repo>`.

No account, no quota and no network once the weights are cached, so replay scoring, calibration
and tuning experiments cost nothing but time. The model answers in the JSON action protocol the
subscription CLI uses, but here the protocol is enforced while decoding: a grammar built from
every action's own argument schema masks each token that would break it, so a reply can't name
an unknown action, give an argument the wrong type or come back as malformed JSON.

Effort maps to thinking: `low` (or none) answers directly; higher efforts think first, at the
model's reasoning-effort level, with a token budget per level. The grammar applies once thinking
ends. Sampling follows the model card, with a fixed seed so a replay is repeatable.

One copy of the weights is loaded per process, on one worker thread that runs every request in
turn: a 27B model leaves no room for a second copy, and MLX streams belong to their thread.
Install with `uv sync --extra mlx`; weights download from Hugging Face on first use.
"""

from __future__ import annotations

import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from ..config import ModelSpec
from ..types import AssistantTurn, ImagePart, Part, TextPart, ToolSpec, Usage
from .base import ActionSession, Provider, action_schema, parse_json_text, tools_text

PROTOCOL = """

## How to act
Reply with one JSON object and nothing else: {{"note": "...", "actions": [{{"tool": "<action name>", "args": {{...}}}}, ...]}}
Actions run in order; the batch stops at the first failure. An empty "actions" list ends your turn, and "note" is then your reply.
Actions (args follow each JSON schema):
{tools}"""

# effort -> (the template's reasoning_effort, thinking-token budget); low answers without thinking.
THINKING: dict[str, tuple[str, int] | None] = {
    "low": None, "medium": ("low", 1024), "high": ("medium", 4096), "xhigh": ("xhigh", 8192), "max": ("xhigh", 16384),
}
# Qwen3.8 model card. Its presence penalty is left out: under a grammar it only pushes the free
# choices, coordinate digits among them, away from values already written.
SAMPLING = {False: {"temperature": 0.7, "top_p": 0.8, "top_k": 20}, True: {"temperature": 1.0, "top_p": 0.95, "top_k": 20}}
MAX_ANSWER = 1024
PREFILL_STEP = 512  # chunked prefill: 2048-token chunks peaked past the GPU working-set limit beside 16 GB of weights
# The model's own JSON style. Compact separators (":" without the space) forced it off the token it
# reaches for, and it derailed: {"note":", ","actions":[],"actions2":null} on an element-id probe.
JSON_STYLE = {"whitespace_flexible": False, "item_separator": ", ", "key_separator": ": ", "lenient": True}
STALE_IMAGE = "[older screenshot removed]"


def tool_grammar(tools: list[ToolSpec]) -> dict[str, Any]:
    """The action schema with each action's argument schema attached, so arguments are checked while decoding."""
    schema = action_schema(tools)
    schema["additionalProperties"] = False
    schema["properties"]["actions"]["items"] = {"anyOf": [
        {"type": "object", "properties": {"tool": {"const": t.name}, "args": t.schema}, "required": ["tool", "args"],
         "additionalProperties": False}
        for t in tools
    ]}
    return schema


@dataclass
class Reply:
    answer: str
    thinking: str
    usage: Usage
    latency: float
    detail: str
    finish: str | None


class Runtime:
    """One loaded model, shared by every provider and session for the same repo."""

    _all: dict[str, "Runtime"] = {}
    _guard = threading.Lock()

    @classmethod
    def get(cls, repo: str) -> "Runtime":
        with cls._guard:
            if repo not in cls._all:
                cls._all[repo] = cls(repo)
            return cls._all[repo]

    def __init__(self, repo: str):
        self.repo = repo
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")
        self.grammars: dict[str, Any] = {}
        self.worker.submit(self._load).result()

    def _load(self) -> None:
        try:
            import mlx.core as mx
            import mlx_vlm
        except ImportError as e:
            raise RuntimeError("Local models need mlx-vlm: run `uv sync --extra mlx`.") from e
        self.mx = mx
        self.model, self.processor = mlx_vlm.load(self.repo)
        self.tokenizer = getattr(self.processor, "tokenizer", self.processor)
        mx.set_cache_limit(1 << 30)  # freed buffers above 1 GB go back to the system, not a private pool

    def _grammar(self, schema: dict[str, Any]) -> Any:
        import llguidance as llg
        import llguidance.hf
        from mlx_vlm.structured import LLGuidanceLogitsProcessor

        key = json.dumps(schema, sort_keys=True)
        if key not in self.grammars:
            if not hasattr(self, "llg_tokenizer"):
                self.llg_tokenizer = llguidance.hf.from_tokenizer(self.tokenizer)
            self.grammars[key] = LLGuidanceLogitsProcessor(
                llg.grammar_from("json_schema", json.dumps({**schema, "x-guidance": JSON_STYLE})), self.llg_tokenizer)
        return self.grammars[key].clone()

    def generate(self, messages: list[dict[str, Any]], images: list[Any], schema: dict[str, Any], effort: str | None) -> Reply:
        return self.worker.submit(self._generate, messages, images, schema, effort).result()

    def _generate(self, messages: list[dict[str, Any]], images: list[Any], schema: dict[str, Any], effort: str | None) -> Reply:
        from mlx_vlm import apply_chat_template, stream_generate
        from mlx_vlm.structured import ThinkingAwareLogitsProcessor

        think = THINKING.get(effort or "low")
        template: dict[str, Any] = {"enable_thinking": think is not None}
        if think:
            template["reasoning_effort"] = think[0]
        prompt = apply_chat_template(self.processor, self.model.config, messages, num_images=len(images), **template)
        grammar = self._grammar(schema)
        if think:
            grammar = ThinkingAwareLogitsProcessor(grammar, self.tokenizer, enable_thinking=True)
        t0 = time.monotonic()
        for attempt in (1, 2):
            text, last = "", None
            try:
                for chunk in stream_generate(
                    self.model, self.processor, prompt, image=images or None, max_tokens=(think[1] if think else 0) + MAX_ANSWER,
                    logits_processors=[grammar], enable_thinking=think is not None, thinking_budget=think[1] if think else None,
                    prefill_step_size=PREFILL_STEP, seed=0, **SAMPLING[think is not None],
                ):
                    text += chunk.text
                    last = chunk
                break
            except RuntimeError as e:
                # Under memory pressure (other apps swapped out beside 16 GB of weights) the GPU watchdog
                # can kill a command buffer. Once is bad luck; twice is the machine, so it surfaces.
                if "[METAL]" not in str(e) or attempt == 2:
                    raise
                grammar = grammar.clone() if hasattr(grammar, "clone") else grammar
            finally:
                self.mx.clear_cache()
        latency = time.monotonic() - t0
        thinking, answer = split_reply(text)
        prompt_tokens = getattr(last, "prompt_tokens", 0)
        cached = getattr(last, "cached_tokens", 0)
        usage = Usage(input_tokens=prompt_tokens - cached, output_tokens=getattr(last, "generation_tokens", 0), cache_read=cached)
        detail = (f"prefill {prompt_tokens} tok at {getattr(last, 'prompt_tps', 0):.0f}/s, decode {usage.output_tokens} tok at "
                  f"{getattr(last, 'generation_tps', 0):.1f}/s, peak {getattr(last, 'peak_memory', 0):.1f} GB")
        return Reply(answer, thinking, usage, latency, detail, getattr(last, "finish_reason", None))


def split_reply(text: str) -> tuple[str, str]:
    """(thinking, answer) from raw output: thinking ends at the last </think>; end-of-turn tokens are dropped."""
    thinking, _, answer = text.rpartition("</think>") if "</think>" in text else ("", "", text)
    for token in ("<|im_end|>", "<|endoftext|>"):
        answer = answer.replace(token, "")
    return thinking.replace("<think>", "").strip(), answer.strip()


def render(messages: list[dict[str, Any]], keep_images: int) -> tuple[list[dict[str, Any]], list[Any]]:
    """Chat messages with image markers, and the images in order; only the newest `keep_images` keep their pixels."""
    from PIL import Image

    total = sum(isinstance(p, ImagePart) for m in messages if isinstance(m["content"], list) for p in m["content"])
    out: list[dict[str, Any]] = []
    images: list[Any] = []
    seen = 0
    for m in messages:
        if not isinstance(m["content"], list):
            out.append(m)
            continue
        content: list[dict[str, Any]] = []
        for p in m["content"]:
            if isinstance(p, ImagePart):
                seen += 1
                if seen > total - keep_images:
                    images.append(Image.open(io.BytesIO(p.data)).convert("RGB"))
                    content.append({"type": "image"})
                else:
                    content.append({"type": "text", "text": STALE_IMAGE})
            elif isinstance(p, TextPart):
                content.append({"type": "text", "text": p.text})
        out.append({"role": m["role"], "content": content})
    return out, images


def to_turn(reply: Reply, model: str) -> AssistantTurn:
    turn = AssistantTurn(usage=reply.usage, latency=reply.latency, model=f"local:{model}", detail=reply.detail,
                         context=reply.usage.input_tokens + reply.usage.cache_read)
    if reply.thinking:
        turn.notes.append(reply.thinking[:600])
    return turn


class MLXProvider(Provider):
    honours = frozenset({"keep_images"})  # images are re-sent every turn; older ones become a placeholder
    native_computer = False

    def __init__(self, spec: ModelSpec, keep_images: int = 3, runtime: Any = None):
        self.spec = spec
        self.keep_images = max(1, keep_images)
        self.runtime = runtime or Runtime.get(spec.model)

    def session(self, system: str, tools: list[ToolSpec]) -> "MLXSession":
        return MLXSession(self, system + PROTOCOL.format(tools=tools_text(tools)), tool_grammar(tools))

    def complete_json(self, system: str, parts: list[Part], schema: dict[str, Any]) -> tuple[dict[str, Any], AssistantTurn]:
        messages, images = render([{"role": "system", "content": system}, {"role": "user", "content": parts}], self.keep_images)
        reply = self.runtime.generate(messages, images, schema, self.spec.effort)
        turn = to_turn(reply, self.spec.model)
        turn.text = reply.answer
        return parse_json_text(reply.answer), turn


class MLXSession(ActionSession):
    """The conversation lives here and is rendered whole each turn (the model keeps no state between calls)."""

    def __init__(self, provider: MLXProvider, system: str, schema: dict[str, Any]):
        self.p = provider
        self.schema = schema
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self.n = 0

    def _turn(self, parts: list[Part]) -> AssistantTurn:
        self.messages.append({"role": "user", "content": list(parts)})
        messages, images = render(self.messages, self.p.keep_images)
        reply = self.p.runtime.generate(messages, images, self.schema, self.p.spec.effort)
        self.messages.append({"role": "assistant", "content": reply.answer})
        turn = to_turn(reply, self.p.spec.model)
        try:
            data = json.loads(reply.answer)
        except json.JSONDecodeError:  # only when the answer budget ran out mid-reply
            data = {"note": reply.answer, "actions": []}
            turn = self._to_turn(data, turn)
            turn.stop = "max_tokens"
            return turn
        return self._to_turn(data, turn)

    def close(self) -> None:
        self.messages = self.messages[:1]
