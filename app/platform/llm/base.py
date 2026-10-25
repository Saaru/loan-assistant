"""Model-agnostic interface the orchestrator talks to.

Each provider keeps the conversation history in its own native message format
(the session only ever uses one provider) and translates tool definitions and
tool results to and from it.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    input: Any


@dataclass
class ToolOutcome:
    id: str
    content: str
    is_error: bool


@dataclass
class Round:
    messages: list  # native assistant message(s) to append to the history
    tool_calls: list[ToolCall]
    stop: str  # end | tool_use | max_tokens | refusal
    model: str
    usage: dict = field(default_factory=dict)


OnText = Callable[[str], Awaitable[None]]
OnToolStart = Callable[[str, str], Awaitable[None]]


class LLMProvider(Protocol):
    name: str
    model: str

    def user_messages(self, content: str) -> list: ...

    def assistant_messages(self, text: str) -> list: ...

    def tool_result_messages(self, outcomes: list[ToolOutcome]) -> list: ...

    async def stream_round(self, system: str, messages: list, tools: list[dict], effort: str,
                           on_text: OnText, on_tool_start: OnToolStart) -> Round: ...

    def friendly_error(self, exc: Exception) -> str | None: ...


def usage_dict(input_tokens=0, output_tokens=0, cache_read=0, cache_write=0) -> dict:
    return {"input_tokens": input_tokens or 0, "output_tokens": output_tokens or 0,
            "cache_read_input_tokens": cache_read or 0, "cache_creation_input_tokens": cache_write or 0}
