"""The subscription provider, driven against a fake `claude` CLI that speaks stream-json."""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap

import pytest

from pilot.config import ModelSpec
from pilot.providers import claude_code as cc
from pilot.types import ImagePart, TextPart, ToolResult, ToolSpec

FAKE_CLI = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    log = open(os.environ["FAKE_CLI_LOG"], "a")
    log.write(json.dumps({{"argv": sys.argv[1:], "env_safe": os.environ.get("CLAUDE_CODE_SAFE_MODE"),
                          "env_mds": os.environ.get("CLAUDE_CODE_DISABLE_CLAUDE_MDS"),
                          "has_key": "ANTHROPIC_API_KEY" in os.environ}}) + "\\n")
    print(json.dumps({{"type": "system", "subtype": "init"}}), flush=True)
    turn = 0
    for line in sys.stdin:
        msg = json.loads(line)
        log.write(json.dumps({{"msg": msg}}) + "\\n"); log.flush()
        turn += 1
        if os.environ.get("FAKE_CLI_MODE") == "native":
            print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t1", "name": "shell",
                              "input": {{"command": "ls ~/PilotEval"}}}}]}}}}), flush=True)
            print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "",
                              "structured_output": {{"note": "tools unavailable", "actions": [{{"tool": "escalate",
                              "args": {{"reason": "No such tool available"}}}}]}}}}), flush=True)
            continue
        if os.environ.get("FAKE_CLI_MODE") == "auth":
            print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                              "result": "Failed to authenticate: OAuth session expired"}}), flush=True)
            continue
        if turn == 1:
            out = {{"note": "pressing Save", "actions": [{{"tool": "ui", "args": {{"target": "Save"}}}},
                                                        {{"tool": "computer", "args": {{"action": "key", "text": "Return"}}}}]}}
        else:
            out = {{"note": "Saved the file.", "actions": []}}
        print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "..."}}]}}}}), flush=True)
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "",
                          "structured_output": out, "usage": {{"input_tokens": 12, "output_tokens": 7,
                          "cache_read_input_tokens": 900}}}}), flush=True)
    """
)


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLI.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    return str(path), log


TOOLS = [ToolSpec("computer", "c", {"type": "object"}), ToolSpec("ui", "u", {"type": "object"}), ToolSpec("done", "d", {"type": "object"}),
         ToolSpec("shell", "s", {"type": "object"}), ToolSpec("escalate", "e", {"type": "object"})]


def test_session_roundtrip(fake_cli):
    cli, log = fake_cli
    prov = cc.ClaudeCodeProvider(ModelSpec("claude-code", "claude-sonnet-5-5", "low"), cli=cli)
    s = prov.session("SYSTEM", TOOLS)
    t1 = s.send([TextPart("Task: save"), ImagePart(b"png", "image/png", 10, 10)])
    assert [c.name for c in t1.calls] == ["ui", "computer"] and t1.calls[0].args == {"target": "Save"}
    assert t1.stop == "tool_use" and t1.notes == ["pressing Save"] and t1.usage.cache_read == 900
    t2 = s.reply(t1.calls, [ToolResult(t1.calls[0].id, True, "pressed", image=ImagePart(b"x")), ToolResult(t1.calls[1].id, True, "OK")])
    assert t2.stop == "end" and t2.text == "Saved the file."
    s.close()

    entries = [json.loads(line) for line in log.read_text().splitlines()]
    start = entries[0]
    argv = start["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--effort") + 1] == "low"
    assert argv[argv.index("--tools") + 1] == "" and "--json-schema" in argv
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["properties"]["actions"]["items"]["properties"]["tool"]["enum"] == ["computer", "ui", "done", "shell", "escalate"]
    assert "How to act" in argv[argv.index("--system-prompt") + 1]
    # personal CLAUDE.md and plugins stay out; the subscription login is used, not an exported key
    assert start["env_safe"] == "1" and start["env_mds"] == "1" and start["has_key"] is False
    first, second = [e["msg"] for e in entries if "msg" in e]
    assert first["message"]["content"][1]["type"] == "image"
    assert "Results:" in second["message"]["content"][0]["text"] and second["message"]["content"][1]["type"] == "image"


def test_auth_error_is_actionable(fake_cli, monkeypatch):
    cli, _ = fake_cli
    monkeypatch.setenv("FAKE_CLI_MODE", "auth")
    s = cc.ClaudeCodeProvider(ModelSpec("claude-code", "opus"), cli=cli).session("S", TOOLS)
    with pytest.raises(cc.CLIError, match="claude auth login"):
        s.send([TextPart("x")])
    s.close()


def test_native_tool_calls_are_rescued(fake_cli, monkeypatch):
    # The CLI answers a native call with "No such tool available"; the model then wrongly escalates.
    cli, _ = fake_cli
    monkeypatch.setenv("FAKE_CLI_MODE", "native")
    s = cc.ClaudeCodeProvider(ModelSpec("claude-code", "sonnet"), cli=cli).session("S", TOOLS)
    t = s.send([TextPart("x")])
    assert [(c.name, c.args) for c in t.calls] == [("shell", {"command": "ls ~/PilotEval"})] and s.rescued == 1
    s.close()
