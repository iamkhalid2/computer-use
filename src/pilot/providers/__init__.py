from __future__ import annotations

from ..config import ModelSpec
from .base import PROVIDER_KNOBS, Provider, Session, cost_of


def inert_knobs(spec: ModelSpec) -> set[str]:
    """Provider-dependent knobs this spec's provider ignores."""
    if spec.provider == "claude-code":
        from .claude_code import ClaudeCodeProvider as cls
    elif spec.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider as cls
    else:
        from .openai_compat import OpenAICompatProvider as cls
    return set(PROVIDER_KNOBS - cls.honours)


def get_provider(spec: ModelSpec, keep_images: int = 3) -> Provider:
    if spec.provider == "claude-code":
        from .claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider(spec)
    if spec.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(spec)
    from .openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(spec, keep_images=keep_images)


__all__ = ["PROVIDER_KNOBS", "Provider", "Session", "cost_of", "get_provider", "inert_knobs"]
