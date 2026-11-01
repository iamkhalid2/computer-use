"""Claude through the official Claude Code CLI, signed in with your own subscription.

Personal use only: Anthropic permits signing in to the unmodified `claude` binary
with your own Pro/Max plan and scripting it (`claude -p`). It does not permit
extracting the OAuth token or offering claude.ai login to other people, so Pilot
never touches credentials: it runs the CLI as a subprocess. Plan usage limits
assume ordinary individual use; benchmark-scale runs belong on an API key.

Each session is one long-lived `claude -p --input-format stream-json` process:
the CLI keeps the conversation (and its prompt cache) between turns, so a turn
costs one model call, not a process start. Built-in tools are disabled and the
model answers with JSON actions validated by `--json-schema`; Pilot executes them.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..config import ModelSpec
from ..types import AssistantTurn, ImagePart, Part, ToolSpec, Usage
from .base import ActionSession, Provider, action_schema, parse_json_text, tools_text

ALIASES = {
    "claude-opus-5-5": "opus", "claude-sonnet-5-5": "sonnet", "claude-haiku-5-5": "haiku", "claude-fable-5-1": "fable",
}

PROTOCOL = """

## How to act
Every action below is available and works. You perform them by listing them in your JSON reply, never by calling a tool:
{{"note": "...", "actions": [{{"tool": "<action name>", "args": {{...}}}}, ...]}}
There are no callable tools in this session, so a direct tool call fails with "No such tool available". That error says
nothing about the action itself: put the same action in the "actions" list instead. Never conclude an action is unavailable.
Actions run in order; the batch stops at the first failure. An empty "actions" list ends your turn.
Actions (args follow each JSON schema):
{tools}"""


def find_cli() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for p in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if p.exists():
            return str(p)
    return None


def cli_env() -> dict[str, str]:
    env = dict(os.environ)
    # Keep the user's CLAUDE.md, hooks, plugins and memory out of the agent's context; keep OAuth.
    env.update({
        "CLAUDE_CODE_SAFE_MODE": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    })
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        # Use the subscription login, not an API key that happens to be exported.
        if os.environ.get("PILOT_CLAUDE_CODE_USE_KEY") != "1":
            env.pop(key, None)
    return env


def auth_status(cli: str | None = None) -> dict[str, Any]:
    cli = cli or find_cli()
    if not cli:
        return {"loggedIn": False, "error": "claude CLI not found"}
    try:
        out = subprocess.run([cli, "auth", "status"], capture_output=True, text=True, timeout=20, env=cli_env())
        return json.loads(out.stdout)
    except Exception as e:
        return {"loggedIn": False, "error": str(e)}


def _content(parts: list[Part]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in parts:
        if isinstance(p, ImagePart):
            out.append({"type": "image", "source": {"type": "base64", "media_type": p.media_type, "data": p.b64}})
        else:
            out.append({"type": "text", "text": p.text})
    return out


class CLIError(RuntimeError):
    pass


class UsageLimit(CLIError):
    """The subscription's session or weekly limit: retrying before the reset is pointless."""


class ClaudeCodeProvider(Provider):
    native_computer = False

    def __init__(self, spec: ModelSpec, cli: str | None = None):
        self.spec = spec
        self.cli = cli or find_cli()
        if not self.cli:
            raise CLIError("The `claude` CLI was not found. Install Claude Code, then run `claude auth login`.")
        self.model = ALIASES.get(spec.model, spec.model)
        self.workdir = tempfile.mkdtemp(prefix="pilot-cc-")  # empty cwd: no project files or settings

    def base_args(self, system: str, schema: dict[str, Any]) -> list[str]:
        args = [
            self.cli, "-p", "--model", self.model, "--tools", "", "--system-prompt", system,
            "--json-schema", json.dumps(schema), "--setting-sources", "", "--no-session-persistence",
            "--permission-prompts", "none", "--strict-mcp-config",
        ]
        if self.spec.effort:
            args += ["--effort", self.spec.effort]
        for fb in self.spec.fallbacks:
            args += ["--fallback-model", ALIASES.get(fb, fb)]
        return args

    def session(self, system: str, tools: list[ToolSpec]) -> "ClaudeCodeSession":
        return ClaudeCodeSession(self, system + PROTOCOL.format(tools=tools_text(tools)), action_schema(tools))

    def complete_json(self, system: str, parts: list[Part], schema: dict[str, Any]) -> tuple[dict[str, Any], AssistantTurn]:
        args = self.base_args(system, schema) + ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        t0 = time.monotonic()
        line = json.dumps({"type": "user", "message": {"role": "user", "content": _content(parts)}})
        proc = subprocess.run(args, input=line + "\n", capture_output=True, text=True, timeout=300, cwd=self.workdir, env=cli_env())
        result = None
        for raw in proc.stdout.splitlines():
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                result = ev
        if result is None:
            raise CLIError(f"claude CLI produced no result (exit {proc.returncode}): {proc.stderr.strip()[:400]}")
        turn = result_to_turn(result, time.monotonic() - t0, self.spec.model)
        data = result.get("structured_output")
        if data is None:
            data = parse_json_text(result.get("result") or "")
        return data, turn


def result_to_turn(result: dict[str, Any], latency: float, model: str) -> AssistantTurn:
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        msg = str(result.get("result") or result.get("subtype") or "error")
        if "authenticate" in msg.lower() or "oauth" in msg.lower() or "login" in msg.lower():
            raise CLIError(f"Claude Code is not signed in ({msg}). Run `claude auth login` in a terminal.")
        if "limit" in msg.lower() and ("hit your" in msg.lower() or "usage" in msg.lower() or "resets" in msg.lower()):
            raise UsageLimit(f"Claude plan usage limit reached: {msg[:200]}")
        raise CLIError(f"claude CLI error: {msg[:400]}")
    u = result.get("usage") or {}
    usage = Usage(
        input_tokens=int(u.get("input_tokens") or 0), output_tokens=int(u.get("output_tokens") or 0),
        cache_read=int(u.get("cache_read_input_tokens") or 0), cache_write=int(u.get("cache_creation_input_tokens") or 0),
    )
    # Subscription usage has no per-call price; leave the model name unknown to the cost table.
    return AssistantTurn(usage=usage, latency=latency, model=f"subscription:{model}")


class ClaudeCodeSession(ActionSession):
    """One persistent CLI process per executor session."""

    def __init__(self, provider: ClaudeCodeProvider, system: str, schema: dict[str, Any]):
        self.p = provider
        args = provider.base_args(system, schema) + ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
            cwd=provider.workdir, env=cli_env(),
        )
        self.stderr: list[str] = []
        # A reader thread + queue: select() on a buffered pipe can miss lines Python already read.
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._drain, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()
        self.n = 0
        self.timeout = float(os.environ.get("PILOT_CLAUDE_CODE_TIMEOUT", "240"))
        self.action_names = set(schema["properties"]["actions"]["items"]["properties"]["tool"].get("enum") or [])
        self.rescued = 0  # native tool calls turned back into actions

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _drain(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr.append(line)
            del self.stderr[:-50]

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass

    def _turn(self, parts: list[Part]) -> AssistantTurn:
        if self.proc.poll() is not None:
            raise CLIError(f"claude CLI exited ({self.proc.returncode}): {''.join(self.stderr)[-400:]}")
        t0 = time.monotonic()
        msg = {"type": "user", "message": {"role": "user", "content": _content(parts)}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        texts: list[str] = []
        context = 0
        native: list[dict[str, Any]] = []  # actions the model tried to call as tools
        while True:
            remaining = self.timeout - (time.monotonic() - t0)
            if remaining <= 0:
                self.proc.kill()
                raise CLIError(f"claude CLI turn timed out after {self.timeout:.0f}s")
            try:
                raw = self.lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if raw is None:
                raise CLIError(f"claude CLI exited ({self.proc.poll()}): {''.join(self.stderr)[-400:]}")
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "system" and ev.get("subtype") == "api_retry" and self.on_note:
                self.on_note(f"API retry {ev.get('attempt')} ({ev.get('error')})")
            elif kind == "assistant":
                u = (ev.get("message") or {}).get("usage") or {}
                # The result's usage sums every request the CLI made for this turn (its own retries
                # included); the context the model actually saw is the largest single request.
                context = max(context, sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        texts.append(block["text"])
                    elif block.get("type") == "tool_use" and block.get("name") in self.action_names:
                        native.append({"tool": block["name"], "args": block.get("input") or {}})
            elif kind == "result":
                turn = result_to_turn(ev, time.monotonic() - t0, self.p.spec.model)
                turn.context = context
                data = ev.get("structured_output")
                if data is None:
                    try:
                        data = parse_json_text(ev.get("result") or "\n".join(texts))
                    except (json.JSONDecodeError, ValueError):
                        data = {"note": ev.get("result") or "", "actions": []}
                if native:
                    # The model's real intent is the calls it tried; its summary after the CLI's
                    # "No such tool" errors is a misreading, so run the intended actions instead.
                    planned = [a for a in data.get("actions") or [] if isinstance(a, dict) and a.get("tool") not in {"escalate", "done"}]
                    if not planned:
                        self.rescued += len(native)
                        data = {"note": "", "actions": native}
                return self._to_turn(data, turn)
