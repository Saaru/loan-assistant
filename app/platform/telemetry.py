"""Per-turn tracing and latency metrics.

Every turn gets a TurnTrace. LLM rounds, tool executions and backend calls record
spans into the trace of the turn they run in (via a context variable, so spans
from parallel tool calls land in the right place).
"""

import json
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Span:
    kind: str  # llm | tool | backend
    name: str
    start_ms: float
    duration_ms: float
    ok: bool = True
    attrs: dict = field(default_factory=dict)


@dataclass
class TurnTrace:
    turn_id: str
    session_id: str
    usecases: list[str]
    channel: str = "text"
    started: float = field(default_factory=time.perf_counter)
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    spans: list[Span] = field(default_factory=list)
    first_token_ms: float | None = None
    first_feedback_ms: float | None = None
    total_ms: float | None = None
    llm_rounds: int = 0
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0,
                                                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def now_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000

    def add_span(self, kind: str, name: str, start_ms: float, ok: bool = True, **attrs) -> Span:
        span = Span(kind, name, round(start_ms, 1), round(self.now_ms() - start_ms, 1), ok, attrs)
        self.spans.append(span)
        return span

    def mark_feedback(self):
        if self.first_feedback_ms is None:
            self.first_feedback_ms = round(self.now_ms(), 1)

    def mark_first_token(self):
        if self.first_token_ms is None:
            self.first_token_ms = round(self.now_ms(), 1)
        self.mark_feedback()

    def finish(self):
        self.total_ms = round(self.now_ms(), 1)

    def summary(self) -> dict:
        data = asdict(self)
        data.pop("started")
        backend = [s for s in self.spans if s.kind == "backend"]
        data["counts"] = {
            "llm_rounds": self.llm_rounds,
            "tools": sum(1 for s in self.spans if s.kind == "tool"),
            "backend_calls": len(backend),
            "backend_failures": sum(1 for s in backend if not s.ok),
            "cache_hits": sum(1 for s in backend if s.attrs.get("cache_hit")),
            "systems": sorted({s.attrs.get("system") for s in backend if s.attrs.get("system")}),
        }
        data["llm_ms"] = round(sum(s.duration_ms for s in self.spans if s.kind == "llm"), 1)
        return data


current_trace: ContextVar[TurnTrace | None] = ContextVar("current_trace", default=None)
# Name of the tool whose task is running, so backend spans can be attributed to it.
current_tool: ContextVar[str | None] = ContextVar("current_tool", default=None)


def record_span(kind: str, name: str, start_ms: float, ok: bool = True, **attrs):
    trace = current_trace.get()
    if trace is not None:
        tool = current_tool.get()
        if tool and kind == "backend":
            attrs.setdefault("tool", tool)
        trace.add_span(kind, name, start_ms, ok, **attrs)


def trace_now_ms() -> float:
    trace = current_trace.get()
    return trace.now_ms() if trace is not None else 0.0


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(p / 100 * len(ordered) + 0.5) - 1))
    return round(ordered[k], 1)


def _stats(values: list[float]) -> dict:
    return {"n": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95),
            "max": round(max(values), 1) if values else None}


class MetricsStore:
    def __init__(self, path: Path, maxlen: int = 2000):
        self.turns: deque[dict] = deque(maxlen=maxlen)
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, trace: TurnTrace) -> dict:
        summary = trace.summary()
        self.turns.append(summary)
        with self.path.open("a") as fh:
            fh.write(json.dumps(summary, default=str) + "\n")
        return summary

    def annotate(self, turn_id: str, values: dict) -> bool:
        """Attach measurements taken elsewhere (e.g. the browser's voice timings) to a finished turn."""
        for turn in reversed(self.turns):
            if turn["turn_id"] == turn_id:
                turn["extra"].update(values)
                with self.path.open("a") as fh:
                    fh.write(json.dumps({"annotate": turn_id, **values}) + "\n")
                return True
        return False

    def recent(self, limit: int = 20) -> list[dict]:
        return list(self.turns)[-limit:][::-1]

    def summary(self) -> dict:
        turns = [t for t in self.turns if not t.get("error")]
        spans = [s for t in self.turns for s in t["spans"]]
        by_tool: dict[str, list[float]] = {}
        by_system: dict[str, list[float]] = {}
        failures: dict[str, int] = {}
        for s in spans:
            if s["kind"] == "tool":
                by_tool.setdefault(s["name"], []).append(s["duration_ms"])
            if s["kind"] == "backend" and not s["attrs"].get("cache_hit"):
                system = s["attrs"].get("system", "?")
                by_system.setdefault(system, []).append(s["duration_ms"])
                if not s["ok"]:
                    failures[system] = failures.get(system, 0) + 1
        return {
            "turns": len(self.turns),
            "errors": sum(1 for t in self.turns if t.get("error")),
            "first_feedback_ms": _stats([t["first_feedback_ms"] for t in turns if t["first_feedback_ms"] is not None]),
            "first_token_ms": _stats([t["first_token_ms"] for t in turns if t["first_token_ms"] is not None]),
            "total_ms": _stats([t["total_ms"] for t in turns if t["total_ms"] is not None]),
            "llm_round_ms": _stats([s["duration_ms"] for s in spans if s["kind"] == "llm"]),
            "voice": {
                "speech_to_text_ms": _stats([t["extra"]["stt_ms"] for t in turns if "stt_ms" in t.get("extra", {})]),
                "end_of_speech_to_first_audio_ms": _stats([t["extra"]["first_audio_ms"] for t in turns
                                                           if "first_audio_ms" in t.get("extra", {})]),
                "end_of_speech_to_answer_audio_ms": _stats([t["extra"]["first_answer_audio_ms"] for t in turns
                                                            if "first_answer_audio_ms" in t.get("extra", {})]),
            },
            "tools": {k: _stats(v) for k, v in sorted(by_tool.items())},
            "systems": {k: {**_stats(v), "failed_attempts": failures.get(k, 0)} for k, v in sorted(by_system.items())},
            "cache_hit_rate": round(
                sum(1 for s in spans if s["kind"] == "backend" and s["attrs"].get("cache_hit"))
                / max(1, sum(1 for s in spans if s["kind"] == "backend")), 3),
        }
