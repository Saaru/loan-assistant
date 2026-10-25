"""Picks the language model provider from configuration.

LA_LLM_PROVIDER = anthropic | groq | gemini | ollama | openai_compatible
If unset, the first provider with a key in the environment is used, in the order
anthropic, groq, gemini.
"""

import os

from ...settings import Settings
from .base import LLMProvider, Round, ToolCall, ToolOutcome  # noqa: F401

PRESETS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "model": "openai/gpt-oss-120b", "fallbacks": ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"],
             "reasoning_effort": True},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "key_env": "GEMINI_API_KEY",
               "model": "gemini-2.5-flash", "fallbacks": [], "reasoning_effort": True},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None, "model": "qwen3:8b", "fallbacks": [],
               "reasoning_effort": False},
    "openai_compatible": {"base_url": None, "key_env": "LA_LLM_API_KEY", "model": None, "fallbacks": [],
                          "reasoning_effort": False},
}


def detect_provider() -> str | None:
    explicit = os.getenv("LA_LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    for name in ("groq", "gemini"):
        if os.getenv(PRESETS[name]["key_env"]):
            return name
    return None


def make_provider(settings: Settings) -> LLMProvider | None:
    name = detect_provider()
    if name is None:
        return None
    model = os.getenv("LA_MODEL", "").strip()
    if name == "anthropic":
        if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
            return None
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(model or "claude-opus-5-5", settings.max_output_tokens)
    if name not in PRESETS:
        raise ValueError(f"Unknown LA_LLM_PROVIDER {name}")
    preset = PRESETS[name]
    key = os.getenv(preset["key_env"], "") if preset["key_env"] else "ollama"
    base_url = os.getenv("LA_LLM_BASE_URL") or preset["base_url"]
    if (preset["key_env"] and not key) or not base_url or not (model or preset["model"]):
        return None
    fallbacks = [m.strip() for m in os.getenv("LA_FALLBACK_MODELS", "").split(",") if m.strip()] or preset["fallbacks"]
    from .openai_provider import OpenAICompatibleProvider
    return OpenAICompatibleProvider(name, model or preset["model"], base_url, key, settings.max_output_tokens,
                                    fallbacks, preset["reasoning_effort"])
