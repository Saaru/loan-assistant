"""The conversation loop with a scripted model: no network, deterministic."""

import json

import httpx
import pytest

from app.platform.actions import ActionManager, AuditLog
from app.platform.connectors import Connectors
from app.platform.llm.base import Round, ToolCall, usage_dict
from app.platform.orchestrator import Orchestrator
from app.platform.policy import PolicyEngine
from app.platform.session import Session
from app.platform.telemetry import MetricsStore
from app.platform.usecase import load_usecases
from app.settings import settings
from finlib.dates import business_date
from mock_systems import app as mock_app, seed

pytestmark = pytest.mark.asyncio


class ScriptedModel:
    """Calls explain_emi_change on the first round, then answers from the tool result."""
    name, model = "scripted", "scripted-1"

    def __init__(self):
        self.seen_tool_result = None

    def user_messages(self, content):
        return [{"role": "user", "content": content}]

    def assistant_messages(self, text):
        return [{"role": "assistant", "content": text}]

    def tool_result_messages(self, outcomes):
        return [{"role": "tool", "tool_call_id": o.id, "content": o.content} for o in outcomes]

    async def stream_round(self, system, messages, tools, effort, on_text, on_tool_start, require_tool=False):
        assert any(t["name"] == "explain_emi_change" for t in tools)
        if messages[-1]["role"] == "user":
            await on_tool_start("call_1", "explain_emi_change")
            return Round([{"role": "assistant", "content": "", "tool_calls": ["..."]}],
                         [ToolCall("call_1", "explain_emi_change", {"loan_id": "LN10001"})], "tool_use", self.model,
                         usage_dict(100, 10))
        self.seen_tool_result = json.loads(messages[-1]["content"])
        change = self.seen_tool_result["change"]
        for part in ("Your EMI went from ", change["from"], " to ", change["to"], " [RR-2]."):
            await on_text(part)
        return Round([{"role": "assistant", "content": "done"}], [], "end", self.model, usage_dict(120, 20))

    def friendly_error(self, exc):
        return None


@pytest.fixture(scope="module", autouse=True)
def fresh_data():
    seed.seed(business_date())


async def test_turn_runs_tools_and_streams_a_grounded_answer(tmp_path):
    connectors = Connectors("http://core", settings.systems_config, transport=httpx.ASGITransport(app=mock_app.app))
    model = ScriptedModel()
    orch = Orchestrator(settings, connectors, PolicyEngine(connectors), ActionManager(AuditLog(tmp_path / "a.jsonl")),
                        MetricsStore(tmp_path / "t.jsonl"), load_usecases(settings.config_dir / "usecases"),
                        business_date, llm=model)
    session = Session("s", "CUST1001", "Priya", ["loan_servicing"])
    events = []

    async def emit(e):
        events.append(e)

    summary = await orch.run_turn(session, "Why did my EMI go up?", emit)
    await connectors.close()

    types = [e["type"] for e in events]
    assert types[0] == "turn_start" and types[-1] == "turn_end"
    assert types.index("tool_pending") < types.index("tool_end") < types.index("text_delta")
    text = "".join(e["text"] for e in events if e["type"] == "text_delta")
    assert text == "Your EMI went from ₹20,969 to ₹21,179 [RR-2]."
    evidence = next(e for e in events if e["type"] == "evidence")["items"]
    assert {"RR-1", "RR-2", "RR-3"} <= {i["id"] for i in evidence if i["type"] == "rule"}
    assert set(model.seen_tool_result["cite"]) == {"RR-1", "RR-2", "RR-3"}  # tool results say which rules apply
    assert summary["counts"]["llm_rounds"] == 2 and summary["counts"]["tools"] == 1
    assert summary["first_feedback_ms"] is not None and summary["first_token_ms"] is not None
    assert [m["role"] for m in session.messages] == ["user", "assistant", "tool", "assistant"]
