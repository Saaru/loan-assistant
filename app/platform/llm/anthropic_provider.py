"""Claude via the Anthropic SDK: streaming, parallel tool use, prompt caching,
effort control and server-side refusal fallback."""

import anthropic

from .base import OnText, OnToolStart, Round, ToolCall, ToolOutcome, usage_dict

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, max_tokens: int):
        self.model = model
        self.max_tokens = max_tokens
        self.client = anthropic.AsyncAnthropic(max_retries=2)

    def user_messages(self, content: str) -> list:
        return [{"role": "user", "content": content}]

    def assistant_messages(self, text: str) -> list:
        return [{"role": "assistant", "content": text}]

    def tool_result_messages(self, outcomes: list[ToolOutcome]) -> list:
        # All results from one round go back in a single user message.
        return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": o.id, "content": o.content,
                                              "is_error": o.is_error} for o in outcomes]}]

    async def stream_round(self, system: str, messages: list, tools: list[dict], effort: str,
                           on_text: OnText, on_tool_start: OnToolStart, require_tool: bool = False) -> Round:
        # Forced tool choice is not available on current Claude models; the switch result's note asks instead.
        tool_params = [{**t, "eager_input_streaming": True} for t in tools]
        for attempt in range(3):
            try:
                async with self.client.beta.messages.stream(
                    model=self.model, max_tokens=self.max_tokens, system=system, messages=messages,
                    tools=tool_params, output_config={"effort": effort}, cache_control={"type": "ephemeral"},
                    betas=[FALLBACK_BETA], fallbacks="default",
                ) as stream:
                    async for event in stream:
                        if event.type == "text":
                            await on_text(event.text)
                        elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                            await on_tool_start(event.content_block.id, event.content_block.name)
                    final = await stream.get_final_message()
                break
            except ValueError:
                # Eager input streaming produced JSON the SDK could not parse; re-issue the round.
                if attempt == 2:
                    raise
        u = final.usage
        usage = usage_dict(u.input_tokens, u.output_tokens, getattr(u, "cache_read_input_tokens", 0),
                           getattr(u, "cache_creation_input_tokens", 0))
        if final.stop_reason == "refusal":
            return Round([], [], "refusal", final.model, usage)
        content = _drop_blocks_before_fallback(final.content)
        calls = [ToolCall(b.id, b.name, b.input) for b in content if b.type == "tool_use"]
        stop = "max_tokens" if final.stop_reason == "max_tokens" else ("tool_use" if calls else "end")
        return Round([{"role": "assistant", "content": content}], calls, stop, final.model, usage)

    def friendly_error(self, exc: Exception) -> str | None:
        if isinstance(exc, anthropic.AuthenticationError):
            return "The model provider rejected the API key. Check ANTHROPIC_API_KEY in .env."
        if isinstance(exc, anthropic.RateLimitError):
            return "The assistant is handling a lot of requests right now. Please try again in a moment."
        if isinstance(exc, (anthropic.APIConnectionError, anthropic.APIStatusError)):
            return "I couldn't reach the language model just now. Please try again."
        return None


def _drop_blocks_before_fallback(content: list) -> list:
    """After a mid-output model fallback, blocks before the last fallback marker
    other than text must not be echoed back."""
    markers = [i for i, b in enumerate(content) if b.type == "fallback"]
    if not markers:
        return list(content)
    cut = markers[-1]
    return [b for i, b in enumerate(content)
            if i >= cut or b.type not in ("thinking", "redacted_thinking", "tool_use", "server_tool_use")]
