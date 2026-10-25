"""Settings, model specs and presets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .profile import Profile

PROVIDER_DEFAULTS = {
    # provider: (base_url, api-key env vars)
    "anthropic": (None, ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")),
    "claude-code": (None, ()),  # your own Claude subscription via the official CLI (personal use)
    "openai": ("https://api.openai.com/v1", ("OPENAI_API_KEY",)),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    "openrouter": ("https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",)),
    "groq": ("https://api.groq.com/openai/v1", ("GROQ_API_KEY",)),
    "ollama": ("http://localhost:11434/v1", ()),
    "lmstudio": ("http://localhost:1234/v1", ()),
}


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    effort: str | None = None  # low | medium | high | xhigh | max (provider-mapped)
    thinking: str = "auto"  # auto | off
    base_url: str | None = None
    fallbacks: tuple[str, ...] = ()  # same-provider models to fail over to on overload/quota/timeouts
    coords: str = "auto"  # auto | pixels | norm1000 (set by calibration)

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def coord_space(self) -> str:
        """How this model family expresses screen coordinates."""
        override = os.environ.get("PILOT_COORDS")
        if override in {"pixels", "norm1000"}:
            return override
        if self.coords in {"pixels", "norm1000"}:
            return self.coords
        m = self.model.lower()
        if self.provider != "anthropic" and ("gemini" in m or "qwen3" in m or "gemma" in m):
            return "norm1000"
        return "pixels"

    def api_key(self) -> str | None:
        envs = PROVIDER_DEFAULTS.get(self.provider, (None, ()))[1]
        for e in envs:
            if os.environ.get(e):
                return os.environ[e]
        return os.environ.get("PILOT_API_KEY")


def parse_spec(text: str, effort: str | None = None, thinking: str = "auto") -> ModelSpec:
    """'anthropic:claude-haiku-5-5', 'gemini:gemini-3.8-flash', 'claude-opus-5-5',
    'ollama:qwen3-vl', 'compat:my-model@http://localhost:8000/v1'."""
    text = text.strip()
    base_url = None
    if ":" in text and not text.startswith(("http://", "https://")):
        provider, model = text.split(":", 1)
    else:
        provider, model = "", text
    if "@" in model:
        model, base_url = model.split("@", 1)
    provider = provider.lower()
    if not provider:
        m = model.lower()
        if m.startswith("claude"):
            provider = "anthropic"
        elif m.startswith(("gemini", "gemma")):
            provider = "gemini"
        elif m.startswith(("gpt", "o1", "o3", "o4", "o5", "computer-use")):
            provider = "openai"
        else:
            raise ValueError(f"can't infer provider for {text!r}; write it as provider:model")
    if provider == "compat":
        if not base_url:
            raise ValueError("compat models need a base URL: compat:model@http://host/v1")
    elif provider not in PROVIDER_DEFAULTS:
        raise ValueError(f"unknown provider {provider!r} (known: {', '.join(PROVIDER_DEFAULTS)}, compat)")
    return ModelSpec(provider, model, effort, thinking, base_url)


# Presets are chosen from Artificial Analysis data (Oct 2026): intelligence index vs
# time-to-first-token (TTFT) and end-to-end time on their standard prompts.
#   executor: called 10-30x per task, so TTFT dominates. Sonnet 5.5 low/medium:
#             TTFT 1.0-1.4 s at index 36-41 is the best fast profile measured.
#             Haiku 5.5 thinks first (TTFT 7.5 s even at low), so it is not the fast pick.
#   planner:  called 0-3x per task. Opus 5.5 medium (index 51, 21.7 s e2e) is on the
#             Pareto frontier; Fable 5.1 low (index 47, TTFT 1.7 s) is the fastest
#             strong reasoner; GPT/Gemini equivalents work through `--planner`.
# The decisive measure is still task success per minute on `pilot eval`, not the index.
PRESETS: dict[str, dict[str, ModelSpec | None]] = {
    "balanced": {
        "planner": ModelSpec("anthropic", "claude-opus-5-5", "medium"),
        "executor": ModelSpec("anthropic", "claude-sonnet-5-5", "low", "off"),
    },
    "fast": {
        "planner": ModelSpec("anthropic", "claude-fable-5-1", "low"),
        "executor": ModelSpec("anthropic", "claude-sonnet-5-5", "low", "off"),
    },
    "lite": {
        "planner": ModelSpec("anthropic", "claude-sonnet-5-5", "medium"),
        "executor": ModelSpec("anthropic", "claude-haiku-5-5", "low", "off"),
    },
    # One strong model does everything (the classic single-loop agent), for comparison.
    "solo": {
        "planner": None,
        "executor": ModelSpec("anthropic", "claude-opus-5-5", "medium"),
    },
    # Your own Claude subscription through the official Claude Code CLI (personal use).
    "sub": {
        "planner": ModelSpec("claude-code", "claude-opus-5-5", "medium"),
        "executor": ModelSpec("claude-code", "claude-sonnet-5-5", "low"),
    },
    "sub-solo": {
        "planner": None,
        "executor": ModelSpec("claude-code", "claude-opus-5-5", "medium"),
    },
    # Gemini Pro needs a paid quota; the planner fails over to Flash when it is missing or busy.
    "gemini": {
        "planner": ModelSpec("gemini", "gemini-pro-latest", "medium", fallbacks=("gemini-3.8-flash", "gemini-3.5-flash")),
        "executor": ModelSpec("gemini", "gemini-3.8-flash", "low", fallbacks=("gemini-3.5-flash", "gemini-3.1-flash-lite")),
    },
    "gemini-fast": {
        "planner": ModelSpec("gemini", "gemini-3.8-flash", "low", fallbacks=("gemini-3.5-flash",)),
        "executor": ModelSpec("gemini", "gemini-3.1-flash-lite", "low", fallbacks=("gemini-3.5-flash",)),
    },
}


@dataclass
class Settings:
    planner: ModelSpec | None
    executor: ModelSpec
    preset: str = "custom"
    # Everything tunable lives in the profile; Settings keeps the invariants and the user's limits.
    profile: "Profile" = field(default_factory=lambda: _default_profile())
    approve: str = "risky"  # risky | all | none
    gui_only: bool = False
    jev: bool = False
    max_turns: int = 80  # executor model calls across the whole task
    max_minutes: float = 15.0
    max_cost: float = 3.0  # USD, only enforced for models with known pricing
    # Folders write_file may change without asking (PILOT_WRITE_ROOTS, colon-separated).
    write_roots: list[str] = field(default_factory=lambda: [r for r in os.environ.get("PILOT_WRITE_ROOTS", "").split(":") if r])
    speak: bool = False
    interactive: bool = True
    runs_dir: Path = field(default_factory=lambda: Path(os.environ.get("PILOT_RUNS", "runs")))

    def with_(self, **kw) -> "Settings":
        return replace(self, **kw)


_CC_LOGGED_IN: bool | None = None


def has_credentials(provider: str) -> bool:
    global _CC_LOGGED_IN
    if provider in {"ollama", "lmstudio", "compat"}:
        return True
    if provider == "claude-code":
        if _CC_LOGGED_IN is None:
            from .providers.claude_code import auth_status

            _CC_LOGGED_IN = bool(auth_status().get("loggedIn"))
        return _CC_LOGGED_IN
    return any(os.environ.get(e) for e in PROVIDER_DEFAULTS[provider][1])


def default_preset() -> str:
    if has_credentials("anthropic"):
        return "balanced"
    if has_credentials("claude-code"):
        return "sub"
    if has_credentials("gemini"):
        return "gemini"
    return "balanced"


def load_dotenv(*paths: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; never overrides the real environment."""
    for p in paths:
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


def _default_profile() -> "Profile":
    from .profile import Profile

    return Profile()


def preset_profile(name: str) -> "Profile":
    """A preset is just a starting profile: its models and efforts, everything else default."""
    from .profile import Profile

    p = PRESETS[name]
    pl, ex = p["planner"], p["executor"]
    assert ex is not None
    values: dict = {"planner": pl.label if pl else None, "executor": ex.label, "executor_effort": ex.effort or "low",
                    "executor_thinking": ex.thinking, "executor_fallbacks": list(ex.fallbacks)}
    if pl:
        values.update(planner_effort=pl.effort or "medium", planner_fallbacks=list(pl.fallbacks))
    return Profile(name, values, {"planner": "preset", "executor": "preset"})


def specs_from_profile(profile: "Profile") -> tuple[ModelSpec | None, ModelSpec]:
    ex = replace(parse_spec(profile.executor), effort=profile.executor_effort, thinking=profile.executor_thinking,
                 fallbacks=tuple(profile.executor_fallbacks), coords=profile.executor_coords)
    pl = None
    if profile.planner:
        pl = replace(parse_spec(profile.planner), effort=profile.planner_effort, fallbacks=tuple(profile.planner_fallbacks))
    return pl, ex


def build_settings(
    preset: str | None = None,
    planner: str | None = None,
    executor: str | None = None,
    planner_effort: str | None = None,
    executor_effort: str | None = None,
    profile: str | None = None,
    **overrides,
) -> Settings:
    """Settings from a saved profile (--profile) or a preset, plus command-line overrides.

    Overrides that name a knob change the profile; the rest are invariants and limits."""
    from .profile import KNOBS, Profile

    if profile:
        prof = Profile.load(profile)
        label = prof.name
    else:
        name = preset or os.environ.get("PILOT_PRESET") or default_preset()
        if name not in PRESETS:
            raise ValueError(f"unknown preset {name!r} (choose from {', '.join(PRESETS)})")
        prof, label = preset_profile(name), name
    changes: dict = {k: v for k, v in overrides.items() if k in KNOBS and v is not None}
    if planner:
        changes["planner"] = None if planner.lower() in {"none", "off", "-"} else planner
    if executor:
        changes.update(executor=executor, executor_fallbacks=[])
    if planner_effort:
        changes["planner_effort"] = planner_effort
    if executor_effort:
        changes["executor_effort"] = executor_effort
    if changes:
        prof = prof.mutate(why="command-line override", **changes)
        if planner or executor:
            label = f"{label}+custom"
    pl, ex = specs_from_profile(prof)
    rest = {k: v for k, v in overrides.items() if k not in KNOBS and v is not None}
    return Settings(planner=pl, executor=ex, preset=label, profile=prof, **rest)


def settings_from_profile(profile: "Profile", base: Settings | None = None) -> Settings:
    """Rebuild settings around a (mutated) profile, keeping the base's invariants and limits."""
    pl, ex = specs_from_profile(profile)
    if base is None:
        return Settings(planner=pl, executor=ex, preset=profile.name, profile=profile)
    return replace(base, planner=pl, executor=ex, profile=profile, preset=profile.name)
