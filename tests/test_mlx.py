"""The local MLX provider, checked with a fake runtime (no weights, no GPU)."""

from __future__ import annotations

import io
import json

from PIL import Image

from pilot.config import parse_spec
from pilot.providers import get_provider, inert_knobs
from pilot.providers.base import cost_of
from pilot.providers.mlx_local import STALE_IMAGE, MLXProvider, Reply, split_reply, tool_grammar
from pilot.types import ImagePart, TextPart, ToolResult, ToolSpec, Usage

TOOLS = [
    ToolSpec("ui", "u", {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}),
    ToolSpec("done", "d", {"type": "object", "properties": {"success": {"type": "boolean"}}}),
]


def png(color: int) -> ImagePart:
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), (color, color, color)).save(buf, "PNG")
    return ImagePart(buf.getvalue(), "image/png", 8, 6)


class FakeRuntime:
    def __init__(self, answers: list[str], thinking: str = ""):
        self.answers = list(answers)
        self.thinking = thinking
        self.calls: list[dict] = []

    def generate(self, messages, images, schema, effort):
        self.calls.append({"messages": messages, "images": images, "schema": schema, "effort": effort})
        return Reply(self.answers.pop(0), self.thinking, Usage(input_tokens=900, output_tokens=40, cache_read=100), 2.5, "d", "stop")


def provider(answers: list[str], keep_images: int = 3, effort: str | None = None, thinking: str = "") -> tuple[MLXProvider, FakeRuntime]:
    rt = FakeRuntime(answers, thinking)
    return MLXProvider(parse_spec("mlx:org/model", effort=effort), keep_images=keep_images, runtime=rt), rt


def test_mlx_session_parses_actions_and_prunes_old_screenshots():
    act = json.dumps({"note": "pressing", "actions": [{"tool": "ui", "args": {"id": 3}}]})
    p, rt = provider([act] * 4, keep_images=2, thinking="the Save button is id 3")
    s = p.session("sys", TOOLS)
    t = s.send([TextPart("task"), png(1)])
    assert [(c.name, c.args) for c in t.calls] == [("ui", {"id": 3})] and t.stop == "tool_use"
    assert t.notes == ["the Save button is id 3", "pressing"]  # thinking first, then the note
    assert t.model == "local:org/model" and cost_of(t.model, t.usage) == 0.0 and t.context == 1000
    for n in range(3):
        t = s.reply(t.calls, [ToolResult(t.calls[0].id, True, "pressed", image=png(10 * n))])
    last = rt.calls[-1]
    markers = [c for m in last["messages"] if isinstance(m["content"], list) for c in m["content"] if c.get("type") == "image"]
    stale = [c for m in last["messages"] if isinstance(m["content"], list) for c in m["content"] if c.get("text") == STALE_IMAGE]
    assert len(markers) == len(last["images"]) == 2 and len(stale) == 2
    assert last["images"][-1].getpixel((0, 0)) == (20, 20, 20)  # the newest screenshot is the one kept
    roles = [m["role"] for m in last["messages"]]
    assert roles[:4] == ["system", "user", "assistant", "user"] and roles[-1] == "user"
    assert "Results:\n1. ui" in last["messages"][-1]["content"][0]["text"]
    assert t.calls[0].id == "cc4"  # call ids keep counting across turns


def test_mlx_final_reply_and_truncated_answer():
    p, _ = provider([json.dumps({"note": "Done: saved.", "actions": []}), '{"note": "half'])
    s = p.session("sys", TOOLS)
    t = s.send([TextPart("task")])
    assert t.stop == "end" and t.text == "Done: saved." and not t.calls
    t = s.send([TextPart("again")])
    assert t.stop == "max_tokens" and not t.calls


def test_mlx_complete_json_and_effort_passthrough():
    p, rt = provider([json.dumps({"plan": ["a"]})], effort="medium")
    data, turn = p.complete_json("plan it", [TextPart("task"), png(5)], {"type": "object"})
    assert data == {"plan": ["a"]} and rt.calls[0]["effort"] == "medium" and len(rt.calls[0]["images"]) == 1


def test_tool_grammar_attaches_each_argument_schema():
    g = tool_grammar(TOOLS)
    items = g["properties"]["actions"]["items"]["anyOf"]
    assert [i["properties"]["tool"]["const"] for i in items] == ["ui", "done"]
    assert items[0]["properties"]["args"]["required"] == ["id"] and all(i["additionalProperties"] is False for i in items)
    assert g["required"] == ["note", "actions"] and g["additionalProperties"] is False  # no stray "actions2"


def test_split_reply():
    assert split_reply("<think>\nhmm\n</think>\n\n{\"a\":1}<|im_end|>") == ("hmm", '{"a":1}')
    assert split_reply('{"a":1}') == ("", '{"a":1}')


def test_mlx_spec_and_registry():
    spec = parse_spec("mlx:mlx-community/Qwen3.8-27B-4bit")
    assert spec.provider == "mlx" and spec.model == "mlx-community/Qwen3.8-27B-4bit"
    assert spec.coord_space == "norm1000"  # Qwen3-family default; calibration confirms or overrides
    assert inert_knobs(spec) == set()  # keep_images is honoured
    rt = FakeRuntime([])
    from pilot.providers import mlx_local

    mlx_local.Runtime._all[spec.model] = rt  # type: ignore[assignment]
    try:
        assert get_provider(spec, keep_images=1).runtime is rt
    finally:
        mlx_local.Runtime._all.pop(spec.model)


def test_provider_class_loads_nothing():
    from pilot.providers import mlx_local, provider_class

    before = dict(mlx_local.Runtime._all)
    cls = provider_class(parse_spec("mlx:org/never-downloaded"))
    assert cls is MLXProvider and cls.native_computer is False and mlx_local.Runtime._all == before
