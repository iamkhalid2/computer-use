"""Provider message formats, checked without network access."""

from __future__ import annotations

from types import SimpleNamespace

from pilot.config import ModelSpec
from pilot.providers import anthropic_provider as ap
from pilot.providers import openai_compat as oc
from pilot.types import ImagePart, TextPart, ToolCall, ToolResult, ToolSpec

TOOLS = [ToolSpec("computer", "c", {"type": "object"}), ToolSpec("ui", "u", {"type": "object", "properties": {}})]


def fake_message(*blocks, stop="tool_use"):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=100, cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, model="claude-sonnet-5-5", stop_details=None)


class FakeBeta:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def create(self, **kw):
        self.requests.append(kw)
        return self.responses.pop(0)


def anthropic_provider(responses, model="claude-sonnet-5-5", thinking="off"):
    p = ap.AnthropicProvider.__new__(ap.AnthropicProvider)
    p.spec = ModelSpec("anthropic", model, "low", thinking)
    p.caps = ap.CAPS.get(model, ap.DEFAULT_CAPS)
    p.disabled = set()
    beta = FakeBeta(responses)
    p.client = SimpleNamespace(beta=SimpleNamespace(messages=beta))
    return p, beta


def test_anthropic_toolset_mapping_and_results():
    click = SimpleNamespace(type="tool_use", id="t1", name="left_click", toolset_name="computer",
                            input=SimpleNamespace(model_dump=lambda exclude_none=True: {"coordinate": [5, 6]}))
    ui = SimpleNamespace(type="tool_use", id="t2", name="ui", toolset_name=None, input={"id": 3})
    note = SimpleNamespace(type="thinking", thinking="Clicking the button")
    p, beta = anthropic_provider([fake_message(note, click, ui), fake_message(SimpleNamespace(type="text", text="ok"), stop="end_turn")])
    s = p.session("sys", TOOLS)
    turn = s.send([TextPart("task"), ImagePart(b"x", "image/png", 10, 10)])
    req = beta.requests[0]
    assert req["tools"][0] == {"type": "computer_toolset_20260801"}
    assert req["tools"][1]["name"] == "ui"
    assert req["thinking"] == {"type": "between_tools"}  # Sonnet 5.5 thinking-off form
    assert req["output_config"] == {"effort": "low"}
    assert req["cache_control"] == {"type": "ephemeral"}
    assert "context-management-2025-06-27" in req["betas"]
    assert turn.calls[0].name == "computer" and turn.calls[0].args == {"action": "left_click", "coordinate": [5, 6]} and turn.calls[0].native
    assert turn.calls[1].name == "ui" and not turn.calls[1].native
    assert turn.notes == ["Clicking the button"] and turn.usage.cache_read == 100

    results = [ToolResult("t1", True, "clicked", image=ImagePart(b"y", "image/png", 10, 10), observation="[1] button"),
               ToolResult("t2", False, "nope")]
    s.reply(turn.calls, results, [TextPart("reminder")])
    msgs = beta.requests[1]["messages"]
    assert msgs[1]["role"] == "assistant" and msgs[1]["content"][1] is click  # verbatim, append-only
    blocks = msgs[2]["content"]
    assert blocks[0]["toolset_name"] == "computer" and blocks[0]["content"][0]["type"] == "image"
    assert "toolset_name" not in blocks[1] and blocks[1]["is_error"] is True
    assert blocks[2] == {"type": "text", "text": "reminder"}
    # history is append-only: the first request's messages are an exact prefix of the second
    assert beta.requests[1]["messages"][: len(beta.requests[0]["messages"])] == beta.requests[0]["messages"]


def test_anthropic_opus_cannot_disable_thinking():
    p, _ = anthropic_provider([], model="claude-opus-5-5", thinking="off")
    assert p.thinking()["type"] == "adaptive"
    kw, betas = p.common()
    assert kw["fallbacks"] == "default" and ap.BETA_FALLBACK in betas


def test_anthropic_feature_downgrade():
    import anthropic
    import httpx2 as httpx  # anthropic 2.x is built on httpx2

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError("fallbacks not supported", response=httpx.Response(400, request=req), body=None)
    ok = fake_message(SimpleNamespace(type="text", text="hi"), stop="end_turn")

    class Flaky(FakeBeta):
        def create(self, **kw):
            self.requests.append(kw)
            if len(self.requests) == 1:
                raise err
            return ok

    p, _ = anthropic_provider([], model="claude-opus-5-5", thinking="auto")
    flaky = Flaky([])
    p.client = SimpleNamespace(beta=SimpleNamespace(messages=flaky))
    s = p.session("sys", TOOLS)
    s.send([TextPart("x")])
    assert "fallbacks" not in flaky.requests[1] and ap.BETA_FALLBACK not in flaky.requests[1]["betas"]


def compat_provider(responses, keep_images=2):
    p = oc.OpenAICompatProvider.__new__(oc.OpenAICompatProvider)
    p.spec = ModelSpec("gemini", "gemini-3.8-flash", "low")
    p.keep_images = keep_images
    p.disabled = set()
    p.model = p.spec.model
    p.queue = []
    p.on_switch = None
    calls = []

    def create(**kw):
        calls.append(kw)
        return responses.pop(0)

    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return p, calls


def chat_response(tool_calls=None, content=None, finish="tool_calls"):
    tcs = []
    for i, (name, args) in enumerate(tool_calls or []):
        fn = SimpleNamespace(name=name, arguments=args)
        tcs.append(SimpleNamespace(id=f"x{i}", function=fn, type="function",
                                   model_dump=lambda exclude_none=True, i=i, name=name, args=args: {
                                       "id": f"x{i}", "type": "function", "function": {"name": name, "arguments": args},
                                       "extra_content": {"google": {"thought_signature": "sig"}}}))
    msg = SimpleNamespace(content=content, tool_calls=tcs or None)
    usage = SimpleNamespace(prompt_tokens=50, completion_tokens=5, prompt_tokens_details=SimpleNamespace(cached_tokens=20))
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)], usage=usage, model="gemini-3.8-flash")


def test_compat_roundtrip_signatures_and_image_pruning():
    responses = [chat_response([("computer", '{"action":"screenshot"}')]) for _ in range(4)]
    p, calls = compat_provider(responses, keep_images=2)
    s = p.session("sys", TOOLS)
    t = s.send([TextPart("task"), ImagePart(b"1")])
    assert calls[0]["reasoning_effort"] == "low"
    assert calls[0]["tools"][0]["function"]["name"] == "computer"
    for n in range(3):
        t = s.reply(t.calls, [ToolResult(t.calls[0].id, True, "shot", image=ImagePart(str(n).encode()))])
    last = calls[-1]["messages"]
    # thought signatures survive the round trip
    assistant = [m for m in last if m["role"] == "assistant"][0]
    assert assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "sig"
    # a tool message answers every call before the screenshot follows as a user message
    roles = [m["role"] for m in last]
    assert roles[:3] == ["system", "user", "assistant"] and roles[3] == "tool" and roles[4] == "user"
    images = sum(1 for m in last if m["role"] == "user" for c in m["content"] if c.get("type") == "image_url")
    assert images == 2  # only the newest screenshots keep their pixels
    assert t.usage.input_tokens == 30 and t.usage.cache_read == 20


def test_compat_invalid_json_arguments():
    p, _ = compat_provider([chat_response([("ui", "{not json")])])
    t = p.session("sys", TOOLS).send([TextPart("x")])
    assert "__invalid__" in t.calls[0].args


def test_compat_reasoning_effort_dropped_on_400():
    import httpx2 as httpx
    import openai

    req = httpx.Request("POST", "https://x/v1/chat/completions")
    p, calls = compat_provider([])
    state = {"n": 0}

    def create(**kw):
        calls.append(kw)
        state["n"] += 1
        if state["n"] == 1:
            raise openai.BadRequestError("reasoning_effort is not supported", response=httpx.Response(400, request=req), body=None)
        return chat_response(content="done", finish="stop")

    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    t = p.session("sys", TOOLS).send([TextPart("x")])
    assert t.stop == "end" and "reasoning_effort" not in calls[1]


def test_compat_fails_over_to_next_model():
    import httpx2 as httpx
    import openai

    req = httpx.Request("POST", "https://x/v1/chat/completions")
    p, calls = compat_provider([])
    p.queue = ["backup-flash"]
    switched = []
    p.on_switch = switched.append

    def create(**kw):
        calls.append(kw)
        if kw["model"] == "gemini-3.8-flash":
            raise openai.InternalServerError("high demand", response=httpx.Response(503, request=req), body=None)
        return chat_response(content="done", finish="stop")

    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    t = p.session("sys", TOOLS).send([TextPart("x")])
    assert t.stop == "end" and calls[-1]["model"] == "backup-flash" and p.model == "backup-flash"
    assert switched and "backup-flash" in switched[0]
