"""Any OpenAI-compatible chat API: Groq, Google Gemini, Ollama, OpenRouter, vLLM...

Streams text and tool calls and sends all tool results back. Free tiers limit
tokens per minute per model, so on a rate limit it moves straight to the next
configured model instead of waiting, and it sends earlier turns' tool results in
compact form (the assistant's replies already carry what was learned from them).
"""

import asyncio
import json
import secrets

import openai
from openai import AsyncOpenAI

from .base import OnText, OnToolStart, Round, ToolCall, ToolOutcome, usage_dict

EARLIER_RESULT = '{"note":"result from an earlier turn; the reply that followed it summarises what matters"}'
MAX_WAIT_S = 15  # if every model is rate limited, wait at most this long for one to free up


class OpenAICompatibleProvider:
    def __init__(self, name: str, model: str, base_url: str, api_key: str, max_tokens: int,
                 fallback_models: list[str] | None = None, reasoning_effort: bool = False):
        self.name = name
        self.model = model
        self.models = [model] + [m for m in (fallback_models or []) if m != model]
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        # Retries are handled here (switch model on 429) rather than by the SDK (which would wait).
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key or "not-needed", max_retries=0)

    def user_messages(self, content: str) -> list:
        return [{"role": "user", "content": content}]

    def assistant_messages(self, text: str) -> list:
        return [{"role": "assistant", "content": text}]

    def tool_result_messages(self, outcomes: list[ToolOutcome]) -> list:
        return [{"role": "tool", "tool_call_id": o.id, "content": o.content} for o in outcomes]

    async def stream_round(self, system: str, messages: list, tools: list[dict], effort: str,
                           on_text: OnText, on_tool_start: OnToolStart) -> Round:
        messages = compact_history(messages)
        spoke = {"text": False}

        async def text_once(text: str):
            spoke["text"] = True
            await on_text(text)

        last_error: Exception | None = None
        for attempt in range(2):
            waits = []
            for model in self.models:
                for _ in range(2):
                    try:
                        return await self._round(model, system, messages, tools, effort, text_once, on_tool_start)
                    except openai.RateLimitError as exc:  # raised before any output streams
                        last_error = exc
                        waits.append(_retry_after(exc))
                        break
                    except (openai.APIConnectionError, openai.InternalServerError) as exc:
                        last_error = exc
                        break
                    except openai.APIError as exc:
                        # Open models occasionally emit a malformed tool call that the API rejects mid-stream.
                        # Retry once on the same model, then the next, unless the customer already saw words.
                        last_error = exc
                        if spoke["text"] or "tool call" not in str(exc).lower():
                            raise
            if attempt == 0 and waits and min(waits) <= MAX_WAIT_S:
                await asyncio.sleep(min(waits))
            else:
                break
        raise last_error

    def _params(self, model: str, effort: str) -> dict:
        extra: dict = {}
        if self.reasoning_effort:
            extra["reasoning_effort"] = effort
        if self.name == "groq" and "qwen" in model:
            extra["reasoning_effort"] = "none"  # fastest; tools do the thinking
            extra["extra_body"] = {"reasoning_format": "hidden"}  # keep <think> text out of the answer
        return extra

    async def _round(self, model, system, messages, tools, effort, on_text, on_tool_start) -> Round:
        stream = await self.client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, *messages],
            tools=[{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                     "parameters": t["input_schema"]}} for t in tools],
            max_completion_tokens=self.max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            **self._params(model, effort),
        )
        text: list[str] = []
        calls: dict[int, dict] = {}
        finish, usage, served_by = None, None, model
        async for chunk in stream:
            served_by = getattr(chunk, "model", None) or served_by
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            extra = getattr(chunk, "x_groq", None)
            if isinstance(extra, dict) and extra.get("usage") and usage is None:
                usage = extra["usage"]
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta and delta.content:
                text.append(delta.content)
                await on_text(delta.content)
            for tc in (delta.tool_calls or []) if delta else []:
                slot = calls.setdefault(tc.index, {"id": None, "name": "", "arguments": "", "announced": False})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["arguments"] += tc.function.arguments
                if slot["name"] and not slot["announced"]:
                    slot["id"] = slot["id"] or f"call_{secrets.token_hex(6)}"
                    slot["announced"] = True
                    await on_tool_start(slot["id"], slot["name"])
            if choice.finish_reason:
                finish = choice.finish_reason

        tool_calls, raw_calls = [], []
        for index in sorted(calls):
            slot = calls[index]
            call_id = slot["id"] or f"call_{secrets.token_hex(6)}"
            try:
                args = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": slot["arguments"]}  # fails validation; the model can retry
            tool_calls.append(ToolCall(call_id, slot["name"], args))
            raw_calls.append({"id": call_id, "type": "function",
                              "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"}})
        message = {"role": "assistant", "content": "".join(text)}
        if raw_calls:
            message["tool_calls"] = raw_calls
        stop = "tool_use" if tool_calls else ("max_tokens" if finish == "length" else "end")
        return Round([message], tool_calls, stop, served_by, _usage(usage))

    def friendly_error(self, exc: Exception) -> str | None:
        if isinstance(exc, openai.AuthenticationError):
            return f"The model provider ({self.name}) rejected the API key. Check it in .env."
        if isinstance(exc, openai.RateLimitError):
            return ("All configured models are at their free-tier rate limit. Wait about a minute and ask again.")
        if isinstance(exc, openai.BadRequestError):
            return f"The model provider rejected the request: {getattr(exc, 'message', exc)}"
        if isinstance(exc, (openai.APIConnectionError, openai.APIStatusError)):
            return f"I couldn't reach the language model ({self.name}) just now. Please try again."
        return None


def compact_history(messages: list) -> list:
    """Replace tool results from earlier turns with a short note, except prepared actions (small, and the
    model needs them to confirm what the customer just approved)."""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=0)
    return [({**m, "content": EARLIER_RESULT}
             if m.get("role") == "tool" and i < last_user and "awaiting_customer_confirmation" not in m["content"]
             else m) for i, m in enumerate(messages)]


def _retry_after(exc: openai.RateLimitError) -> float:
    try:
        return float(exc.response.headers.get("retry-after", MAX_WAIT_S + 1))
    except (TypeError, ValueError, AttributeError):
        return MAX_WAIT_S + 1


def _usage(usage) -> dict:
    if usage is None:
        return usage_dict()
    get = (lambda k: usage.get(k)) if isinstance(usage, dict) else (lambda k: getattr(usage, k, None))
    details = get("prompt_tokens_details")
    cached = (details.get("cached_tokens") if isinstance(details, dict) else getattr(details, "cached_tokens", 0)) or 0
    return usage_dict((get("prompt_tokens") or 0) - cached, get("completion_tokens"), cached, 0)
