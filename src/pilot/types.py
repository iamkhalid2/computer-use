"""Provider-neutral data types shared by the environment, tools and models."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ImagePart:
    data: bytes
    media_type: str = "image/png"
    width: int = 0
    height: int = 0

    @property
    def b64(self) -> str:
        return base64.b64encode(self.data).decode()


@dataclass
class TextPart:
    text: str


Part = TextPart | ImagePart


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]
    native: bool = False  # e.g. an Anthropic computer-toolset member


@dataclass
class ToolResult:
    call_id: str
    ok: bool
    text: str
    image: ImagePart | None = None
    observation: str | None = None  # AX outline that accompanies a screenshot


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write


StopKind = Literal["tool_use", "end", "max_tokens", "refusal", "error"]


@dataclass
class AssistantTurn:
    text: str = ""
    notes: list[str] = field(default_factory=list)  # progress updates / thinking summaries
    calls: list[ToolCall] = field(default_factory=list)
    stop: StopKind = "end"
    usage: Usage = field(default_factory=Usage)
    latency: float = 0.0
    model: str = ""
    detail: str = ""
    context: int = 0  # input tokens of the turn's largest single request (0: unknown, use usage)


@dataclass
class Observation:
    image: ImagePart
    app: str = ""
    window: str = ""
    outline: str = ""
    windows: list[str] = field(default_factory=list)
    focused: str = ""
    ax_ms: float = 0.0
    capture_ms: float = 0.0
    hash: str = ""

    def header(self) -> str:
        lines = [f"Frontmost app: {self.app}" + (f' — window "{self.window}"' if self.window else "")]
        if self.focused:
            lines.append(f"Keyboard focus: {self.focused}")
        if self.windows:
            lines.append("Visible windows: " + "; ".join(self.windows[:8]))
        lines.append(f"Screenshot: {self.image.width}x{self.image.height}")
        return "\n".join(lines)

    def text(self) -> str:
        if not self.outline:
            return self.header()
        return f"{self.header()}\nUI elements (use ids with the `ui` tool; @x,y = screenshot coords):\n{self.outline}"
