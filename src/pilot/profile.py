"""The harness genome: every setting a tuner may change, typed and bounded.

A Profile is the complete, explicit description of how Pilot drives a given model:
roles and models, effort, what the model sees, which scaffolding is on, and which
prompt rules it gets. A self-tuning harness mutates profiles, never code.

What is deliberately *not* here, so no tuner can trade it for speed:
  safety policy and classifiers, the consent-dialog and host-terminal guards,
  task-scoped authorisation, the kill switch, the approval mode, the user's spending
  limits (max_minutes, max_cost, max_turns), GUI-only, write roots, the Mac
  environment layer (capture, input, Accessibility, coordinate mapping), and the
  graders. Those live in Settings and the safety code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent.prompts import TUNABLE_RULES


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Knob:
    name: str
    group: str  # topology | models | observation | scaffold | prompt
    kind: str  # bool | int | float | choice | subset | model | models
    default: Any
    doc: str
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()

    def validate(self, value: Any) -> Any:
        k = self.kind
        if k == "bool":
            if isinstance(value, str) and value.lower() in {"true", "false"}:
                value = value.lower() == "true"
            if not isinstance(value, bool):
                raise ProfileError(f"{self.name} must be true or false, not {value!r}")
            return value
        if k in {"int", "float"}:
            try:
                value = int(value) if k == "int" else float(value)
            except (TypeError, ValueError):
                raise ProfileError(f"{self.name} must be a number, not {value!r}") from None
            if (self.lo is not None and value < self.lo) or (self.hi is not None and value > self.hi):
                raise ProfileError(f"{self.name}={value} is outside [{self.lo}, {self.hi}]")
            return value
        if k == "choice":
            if value not in self.choices:
                raise ProfileError(f"{self.name} must be one of {self.choices}, not {value!r}")
            return value
        if k == "subset":
            items = list(value) if isinstance(value, (list, tuple, set)) else None
            if items is None or not set(items) <= set(self.choices):
                raise ProfileError(f"{self.name} must be a subset of {self.choices}")
            return [c for c in self.choices if c in items]  # canonical order
        if k == "model":
            if value in (None, "", "none"):
                if self.name == "executor":
                    raise ProfileError("the executor model is required")
                return None
            from .config import parse_spec

            try:
                parse_spec(str(value))
            except ValueError as e:
                raise ProfileError(f"{self.name}: {e}") from None
            return str(value)
        if k == "models":
            if not isinstance(value, (list, tuple)):
                raise ProfileError(f"{self.name} must be a list of models")
            return [str(v) for v in value]
        raise ProfileError(f"unknown knob kind {k}")


EFFORTS = ("low", "medium", "high", "xhigh", "max")

KNOBS: dict[str, Knob] = {k.name: k for k in [
    # topology: who does what
    Knob("planner", "models", "model", None, "planner model (provider:model), or none for a single-model agent"),
    Knob("executor", "models", "model", "claude-code:claude-sonnet-5-5", "executor model (provider:model)"),
    Knob("planner_effort", "models", "choice", "medium", "planner reasoning effort", choices=EFFORTS),
    Knob("executor_effort", "models", "choice", "low", "executor reasoning effort", choices=EFFORTS),
    Knob("executor_thinking", "models", "choice", "off", "executor thinking: auto, or off where the model allows it", choices=("auto", "off")),
    Knob("executor_coords", "models", "choice", "auto", "how the executor expresses screen coordinates: auto (by model family), pixels, norm1000",
         choices=("auto", "pixels", "norm1000")),
    Knob("planner_fallbacks", "models", "models", [], "same-provider models to fail over to"),
    Knob("executor_fallbacks", "models", "models", [], "same-provider models to fail over to"),
    Knob("plan", "topology", "choice", "lazy", "lazy: act at once, plan when stuck; upfront: plan first", choices=("lazy", "upfront")),
    Knob("verify", "topology", "choice", "auto", "planner's final check: auto (after escalations or long runs), always, never",
         choices=("auto", "always", "never")),
    Knob("max_step_turns", "topology", "int", 14, "executor turns on one step before it must escalate", lo=3, hi=40),
    Knob("max_escalations", "topology", "int", 4, "planner rescues per task", lo=0, hi=8),
    # observation: what the model sees
    Knob("use_ax", "observation", "bool", True, "send the accessibility element list (else screenshots only)"),
    Knob("auto_observe", "observation", "bool", True, "attach a fresh screenshot and element list after each turn with actions"),
    Knob("max_long_edge", "observation", "int", 1568, "screenshot long edge in pixels", lo=640, hi=2576),
    Knob("image_format", "observation", "choice", "png", "screenshot encoding", choices=("png", "jpeg")),
    Knob("keep_images", "observation", "int", 3, "screenshots kept in context (providers that prune client-side)", lo=1, hi=10),
    Knob("outline_max_lines", "observation", "int", 180, "element list: most elements shown", lo=30, hi=500),
    Knob("outline_max_chars", "observation", "int", 8000, "element list: most characters", lo=1500, hi=24000),
    Knob("compact_outline", "observation", "bool", True, "drop duplicate rows (row > cell > text with the same label)"),
    Knob("settle_timeout", "observation", "float", 1.5, "seconds to wait for the screen to stop changing before a screenshot", lo=0.0, hi=4.0),
    # scaffold: harness behaviours a smarter model may not need
    Knob("evidence_gate", "scaffold", "bool", True, "on write tasks, show the disk state before accepting done"),
    Knob("loop_window", "scaffold", "int", 3, "identical turns before a 'change approach' note (0: off)", lo=0, hi=8),
    Knob("budget_warning", "scaffold", "bool", True, "warn two turns before a step's turn budget runs out"),
    Knob("nudge_on_text_end", "scaffold", "bool", True, "when a turn ends without actions, ask once to act or call done"),
    Knob("no_change_note", "scaffold", "bool", True, "tell the model when its actions changed nothing on screen"),
    Knob("await_editable", "scaffold", "bool", True, "before typing, wait for an editable field to take focus"),
    Knob("editable_timeout", "scaffold", "float", 0.6, "seconds to wait for an editable field", lo=0.0, hi=3.0),
    Knob("paste_threshold", "scaffold", "int", 120, "text at least this long is pasted instead of typed", lo=20, hi=2000),
    # prompt: which optional executor rules are included (safety rules always are)
    Knob("executor_rules", "prompt", "subset", list(TUNABLE_RULES), "optional executor prompt rules", choices=tuple(TUNABLE_RULES)),
]}


@dataclass
class Profile:
    name: str = "default"
    values: dict[str, Any] = field(default_factory=dict)
    why: dict[str, str] = field(default_factory=dict)  # knob -> evidence for its value
    parent: str | None = None
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def __post_init__(self) -> None:
        unknown = set(self.values) - set(KNOBS)
        if unknown:
            raise ProfileError(f"not tunable: {', '.join(sorted(unknown))} (safety and limits are not part of a profile)")
        self.values = {k: KNOBS[k].validate(v) for k, v in self.values.items()}

    def __getattr__(self, name: str) -> Any:
        if name in KNOBS:
            return self.values.get(name, KNOBS[name].default)
        raise AttributeError(name)

    def get(self, name: str) -> Any:
        if name not in KNOBS:
            raise ProfileError(f"{name} is not a knob")
        return getattr(self, name)

    def resolved(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in KNOBS}

    def mutate(self, name: str | None = None, why: str = "", **changes: Any) -> "Profile":
        """A new profile with some knobs changed (validated); the original is untouched."""
        values = dict(self.values)
        values.update(changes)
        reasons = dict(self.why)
        for k in changes:
            if why:
                reasons[k] = why
        return Profile(name or self.name, values, reasons, parent=self.name)

    def diff(self, other: "Profile") -> dict[str, tuple[Any, Any]]:
        a, b = self.resolved(), other.resolved()
        return {k: (a[k], b[k]) for k in KNOBS if a[k] != b[k]}

    # -- persistence ----------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "parent": self.parent, "created": self.created, "values": self.resolved(), "why": self.why}

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return path

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        return cls(data.get("name", "profile"), dict(data.get("values") or {}), dict(data.get("why") or {}),
                   data.get("parent"), data.get("created") or datetime.now().isoformat(timespec="seconds"))

    @classmethod
    def load(cls, ref: str | Path) -> "Profile":
        """A path, or a name looked up in ./profiles, ~/.config/pilot/profiles, then the built-ins."""
        p = Path(ref)
        if p.suffix == ".json" and p.exists():
            return cls.from_dict(json.loads(p.read_text()))
        for d in (Path.cwd() / "profiles", Path.home() / ".config" / "pilot" / "profiles", Path(__file__).parent / "profiles"):
            cand = d / f"{ref}.json"
            if cand.exists():
                return cls.from_dict(json.loads(cand.read_text()))
        raise ProfileError(f"no profile named {ref!r}")


def describe() -> str:
    """Human-readable table of every knob, for `pilot profile knobs`."""
    lines = []
    for group in ("models", "topology", "observation", "scaffold", "prompt"):
        lines.append(f"[{group}]")
        for k in KNOBS.values():
            if k.group == group:
                bounds = f" {k.choices}" if k.choices and k.kind != "subset" else (f" [{k.lo}, {k.hi}]" if k.lo is not None else "")
                lines.append(f"  {k.name:20} {k.kind:7}{bounds}  default={k.default!r}\n      {k.doc}")
    return "\n".join(lines)
