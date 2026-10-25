"""Conversation runtime shared by every use case and channel.

One turn: append the customer's message, stream the model's response, run any
tool calls in parallel, feed results back, repeat until the model answers. Emits
events (text deltas, tool progress, action cards, evidence, latency) to the
channel. The model provider is pluggable (see platform/llm).
"""

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable

from typing import Literal

from pydantic import BaseModel, Field, ValidationError, create_model

from ..settings import Settings
from .actions import ActionError, ActionManager
from .connectors import BackendError, Connectors, SystemUnavailable, session_cache
from .llm import LLMProvider, ToolCall, ToolOutcome, make_provider
from .policy import PolicyEngine
from .prompts import build_system_prompt
from .session import Session
from .telemetry import MetricsStore, TurnTrace, current_tool, current_trace
from .tools import EvidenceCollector, ToolContext, ToolError, ToolSpec
from .usecase import UseCase, tools_for

log = logging.getLogger("orchestrator")

Emit = Callable[[dict], Awaitable[None]]


async def _ignore(_event: dict) -> None:
    return None


class Orchestrator:
    def __init__(self, settings: Settings, connectors: Connectors, policy: PolicyEngine, actions: ActionManager,
                 metrics: MetricsStore, usecases: dict[str, UseCase], today_fn, llm: LLMProvider | None = None):
        self.settings = settings
        self.connectors = connectors
        self.policy = policy
        self.actions = actions
        self.metrics = metrics
        self.usecases = usecases
        self.today_fn = today_fn
        self.llm = llm or make_provider(settings)

    def llm_configured(self) -> bool:
        return self.llm is not None

    def context(self, session: Session, emit: Emit = _ignore, channel: str = "text",
                evidence: EvidenceCollector | None = None) -> ToolContext:
        families = list(self.usecases[session.active].policy_families)  # policy search stays within the workspace
        return ToolContext(session=session, connectors=self.connectors, policy=self.policy, actions=self.actions,
                           evidence=evidence or EvidenceCollector(), emit=emit, today=self.today_fn(), channel=channel,
                           extras={"policy_families": families})

    async def run_turn(self, session: Session, content: str, emit: Emit, channel: str = "text",
                       extra: dict | None = None) -> dict:
        async with session.lock:
            usecases = [self.usecases[session.active]]
            trace = TurnTrace(turn_id=f"T-{secrets.token_hex(4)}", session_id=session.session_id[:8],
                              usecases=[session.active], channel=channel, extra=dict(extra or {}))
            trace_token = current_trace.set(trace)
            cache_token = session_cache.set(session.cache)
            evidence = EvidenceCollector()
            ctx = self.context(session, emit, channel, evidence)
            history_len = len(session.messages)
            executed_before = {a.action_id for a in session.actions.values() if a.status == "executed"}
            session.turns += 1
            await emit({"type": "turn_start", "turn_id": trace.turn_id})
            try:
                if self.llm is None:
                    raise RuntimeError("No language model is configured. Add GROQ_API_KEY (free), GEMINI_API_KEY "
                                       "or ANTHROPIC_API_KEY to .env and restart.")
                pending = self.actions.pending(session)
                if pending:
                    content += "\n\n[App note, not from the customer: awaiting the customer's confirmation: " + "; ".join(
                        f"{a.action_id} {a.title} ({'OTP needed' if a.requires_otp else 'no OTP'})" for a in pending) + "]"
                session.messages.extend(self.llm.user_messages(content))
                await self._loop(session, usecases, ctx, trace, emit, channel)
            except asyncio.CancelledError:
                # Interrupted (voice barge-in or an explicit cancel). Keep only what the customer actually got:
                # their message and the part of the reply already streamed, marked as cut off.
                trace.error = "cancelled"
                del session.messages[history_len:]
                spoken = "".join(trace.extra.get("streamed", []))
                session.messages.extend(self.llm.user_messages(content))
                session.messages.extend(self.llm.assistant_messages(
                    (spoken + " " if spoken else "") + "[reply cut off: the customer interrupted]"))
                raise
            except Exception as exc:  # keep the conversation usable whatever happened
                log.exception("turn failed")
                trace.error = f"{type(exc).__name__}: {exc}"
                del session.messages[history_len:]  # history stays valid: the failed turn is dropped
                done = [a for a in session.actions.values()
                        if a.status == "executed" and a.action_id not in executed_before]
                if done and self.llm is not None:
                    # An action already went through before the failure: say so from its actual result.
                    note = " ".join(f"{a.title} is done" + (f" (reference {a.result['request_id']})."
                                    if (a.result or {}).get("request_id") else ".") for a in done)
                    session.messages.extend(self.llm.user_messages(content))
                    session.messages.extend(self.llm.assistant_messages(note))
                    await emit({"type": "text_delta", "text": note})
                else:
                    await emit({"type": "error", "message": self._friendly_error(exc)})
            finally:
                trace.finish()
                trace.extra.pop("streamed", None)
                summary = self.metrics.add(trace)
                current_trace.reset(trace_token)
                session_cache.reset(cache_token)
            await emit({"type": "evidence", "items": evidence.items})
            turn_metrics = {k: summary[k] for k in ("turn_id", "first_feedback_ms", "first_token_ms", "total_ms",
                                                     "llm_ms", "counts", "usage", "error", "extra")}
            turn_metrics["spans"] = summary["spans"]
            turn_metrics["model"] = self.llm.model if self.llm else None
            await emit({"type": "turn_end", "metrics": turn_metrics})
            return summary

    def _plan(self, session: Session, ctx: ToolContext, channel: str):
        """Tools, prompt and effort for the conversation's active workspace."""
        active = self.usecases[session.active]
        specs = tools_for([active])
        others = [self.usecases[u] for u in session.usecases if u != session.active]
        if others:
            specs = specs + [self._switch_tool(others)]
        system = build_system_prompt(self.settings, [active], session.context_text, ctx.today, channel, others)
        return specs, {s.name: s for s in specs}, [s.schema() for s in specs], system, active.effort

    def _switch_tool(self, others: list[UseCase]) -> ToolSpec:
        """Lets the model move the conversation to another workspace mid-turn; its tools load next round."""
        ids = tuple(uc.id for uc in others)
        model = create_model("SwitchArgs", workspace=(Literal[ids], Field(..., description="Workspace to switch to")))
        usecases = self.usecases

        async def handler(ctx: ToolContext, args) -> dict:
            ctx.session.active = args.workspace
            target = usecases[args.workspace]
            await ctx.emit({"type": "workspace_switched", "usecase": target.public()})
            return {"switched_to": target.name, "note": "Its tools are available now. Continue with the request."}

        names = "; ".join(f"{uc.id} for {uc.tagline.lower()}" for uc in others)
        return ToolSpec("switch_workspace", f"Required before answering anything about another workspace's topics, "
                        f"because their account tools only load after switching: {names}.", model, handler,
                        "Switching workspace", "read")

    async def _loop(self, session: Session, usecases: list[UseCase], ctx: ToolContext, trace: TurnTrace, emit: Emit,
                    channel: str = "text"):
        planned_for = session.active
        specs, allowed, tools, system, effort = self._plan(session, ctx, channel)
        streamed = trace.extra.setdefault("streamed", [])
        state = {"text_started": False, "separator": False}

        async def on_text(text: str):
            if not state["text_started"] and state["separator"]:
                await emit({"type": "text_delta", "text": "\n\n"})
            state["text_started"] = True
            trace.mark_first_token()
            streamed.append(text)
            await emit({"type": "text_delta", "text": text})

        async def on_tool_start(call_id: str, name: str):
            spec = allowed.get(name)
            trace.mark_feedback()
            await emit({"type": "tool_pending", "id": call_id, "name": name, "label": spec.label if spec else name})

        for round_no in range(1, self.settings.max_tool_rounds + 1):
            start = trace.now_ms()
            first_token_before = trace.first_token_ms
            state["text_started"] = False
            result = await self.llm.stream_round(system, session.messages, tools, effort, on_text, on_tool_start)
            state["separator"] = state["separator"] or state["text_started"]
            for key in trace.usage:
                trace.usage[key] += result.usage.get(key, 0)
            trace.llm_rounds += 1
            trace.add_span("llm", f"round {round_no}", start, model=result.model, stop=result.stop,
                           ttft_ms=(round(trace.first_token_ms - start, 1)
                                    if first_token_before is None and trace.first_token_ms is not None else None),
                           input_tokens=result.usage.get("input_tokens"), output_tokens=result.usage.get("output_tokens"),
                           cache_read=result.usage.get("cache_read_input_tokens"))
            if result.stop == "refusal":
                session.messages.extend(self.llm.assistant_messages("I'm not able to help with that request."))
                await emit({"type": "text_delta", "text": "I'm not able to help with that request."})
                return
            session.messages.extend(result.messages)
            if not result.tool_calls:
                return
            if result.stop == "max_tokens":
                raise RuntimeError("The response was cut off while calling a tool")
            outcomes = await asyncio.gather(*(self._run_tool(ctx, call, allowed, trace) for call in result.tool_calls))
            session.messages.extend(self.llm.tool_result_messages(list(outcomes)))
            if session.active != planned_for:  # the model switched workspace: load that workspace's tools
                planned_for = session.active
                specs, allowed, tools, system, effort = self._plan(session, ctx, channel)
                trace.usecases.append(session.active)
        note = "That needed more steps than I can take in one go. Could you narrow the question down a little?"
        session.messages.extend(self.llm.assistant_messages(note))
        await emit({"type": "text_delta", "text": "\n\n" + note})

    async def _run_tool(self, ctx: ToolContext, call: ToolCall, allowed: dict[str, ToolSpec],
                        trace: TurnTrace) -> ToolOutcome:
        spec = allowed.get(call.name)
        start = trace.now_ms()
        if spec is None:
            return ToolOutcome(call.id, json.dumps({"error": f"Tool {call.name} is not available here."}), True)
        tool_token = current_tool.set(spec.name)
        await ctx.emit({"type": "tool_start", "id": call.id, "name": spec.name, "label": spec.label})
        degraded = False
        try:
            if not isinstance(call.input, dict):
                raise ToolError("Tool input was not a JSON object.")
            args = spec.input_model.model_validate(call.input)
            result, is_error = await asyncio.wait_for(spec.handler(ctx, args), self.settings.tool_timeout_s), False
        except ValidationError as exc:
            result, is_error = {"error": "invalid_input", "details": exc.errors(include_url=False, include_context=False)}, True
        except (ToolError, ActionError) as exc:
            result, is_error = {"error": str(exc)}, True
        except SystemUnavailable as exc:
            degraded = True
            result, is_error = {"error": "system_unavailable", "system": exc.display_name, "message": str(exc),
                                "guidance": "Tell the customer this part is temporarily unavailable, answer what "
                                            "you can from other results, and offer to retry."}, True
        except TimeoutError:
            degraded = True
            result, is_error = {"error": "timeout", "message": f"{spec.name} took longer than "
                                f"{self.settings.tool_timeout_s:.0f}s, so it was stopped.",
                                "guidance": "Tell the customer this lookup is slow right now, answer what you can, "
                                            "and offer to try again."}, True
        except BackendError as exc:
            result, is_error = {"error": str(exc), "system": exc.display_name}, True
        except Exception:
            log.exception("tool %s failed", spec.name)
            result, is_error = {"error": f"Internal error in {spec.name}"}, True
        finally:
            current_tool.reset(tool_token)
        span = trace.add_span("tool", spec.name, start, ok=not is_error, degraded=degraded)
        systems = sorted({s.attrs["system"] for s in trace.spans
                          if s.kind == "backend" and s.attrs.get("tool") == spec.name and s.start_ms >= start - 1})
        await ctx.emit({"type": "tool_end", "id": call.id, "name": spec.name, "label": spec.label,
                        "ms": span.duration_ms, "ok": not is_error, "degraded": degraded,
                        "systems": [self.connectors[s].display_name for s in systems]})
        return ToolOutcome(call.id, json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")), is_error)

    def _friendly_error(self, exc: Exception) -> str:
        if self.llm is not None:
            message = self.llm.friendly_error(exc)
            if message:
                return message
        if isinstance(exc, RuntimeError):
            return str(exc)
        return "Something went wrong on my side. Please try again."
