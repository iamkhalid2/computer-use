from __future__ import annotations

from ..config import ModelSpec
from .base import Provider, Session, cost_of


def get_provider(spec: ModelSpec, keep_images: int = 3) -> Provider:
    if spec.provider == "claude-code":
        from .claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider(spec)
    if spec.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(spec)
    from .openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(spec, keep_images=keep_images)


__all__ = ["Provider", "Session", "cost_of", "get_provider"]
