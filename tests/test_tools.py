"""Tools and actions end to end against the simulated core systems (in-process, no LLM).

Covers the guarantees that matter most: answers are computed from the customer's
own data, other customers' accounts are unreachable, actions need confirmation
(and an OTP when high-impact), and confirmed actions run exactly once.
"""

import httpx
import pytest

from app.platform.actions import ActionError, ActionManager, AuditLog
from app.platform.connectors import Connectors, session_cache
from app.platform.orchestrator import Orchestrator
from app.platform.policy import PolicyEngine
from app.platform.session import Session
from app.platform.telemetry import MetricsStore
from app.platform.tools import REGISTRY, ToolError
from app.platform.usecase import load_usecases
from app.settings import settings
from finlib.dates import business_date
from mock_systems import app as mock_app, seed

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module", autouse=True)
def fresh_data():
    assert "tests" in str(seed.DB_PATH), "tests must not touch the demo database"
    seed.seed(business_date())


@pytest.fixture
async def orch(tmp_path):
    connectors = Connectors("http://core", settings.systems_config, transport=httpx.ASGITransport(app=mock_app.app))
    usecases = load_usecases(settings.config_dir / "usecases")
    o = Orchestrator(settings, connectors, PolicyEngine(connectors), ActionManager(AuditLog(tmp_path / "audit.jsonl")),
                     MetricsStore(tmp_path / "telemetry.jsonl"), usecases, business_date)
    yield o
    await connectors.close()


async def call(orch, session, name, **kwargs):
    spec = REGISTRY[name]
    events = []

    async def emit(e):
        events.append(e)

    token = session_cache.set(session.cache)
    try:
        return await spec.handler(orch.context(session, emit), spec.input_model.model_validate(kwargs)), events
    finally:
        session_cache.reset(token)


def session(customer_id, usecase="loan_servicing"):
    return Session(f"test-{customer_id}", customer_id, customer_id, [usecase])


async def test_emi_change_is_explained_from_the_rate_reset(orch):
    out, _ = await call(orch, session("CUST1001"), "explain_emi_change", loan_id="LN10001")
    assert out["changed"] and out["cause"]["type"] == "Floating rate reset"
    assert out["change"]["from"] == "₹20,969" and out["change"]["to"] == "₹21,179"
    assert "matches" in out["cause"]["recalculation_check"]


async def test_other_customers_loans_are_unreachable(orch):
    with pytest.raises(ToolError):
        await call(orch, session("CUST1003"), "get_loans", loan_id="LN10002")


async def test_failed_payments_flag_the_reversible_charge(orch):
    out, _ = await call(orch, session("CUST1002"), "get_payment_history", failed_only=True)
    assert out["summary"]["failed_auto_debits"] == 3
    assert out["summary"]["charges_eligible_for_reversal"] == ["CHG10002020A"]
    assert out["mandate_warning"]


async def test_prepayment_needs_otp_and_executes_once(orch):
    s = session("CUST1001")
    proposal, events = await call(orch, s, "request_prepayment", loan_id="LN10001", amount=50000)
    assert proposal["status"] == "awaiting_customer_confirmation" and proposal["requires_otp"]
    assert "demo_otp" not in str(proposal)  # the model never sees the OTP
    pending = s.actions[proposal["action_id"]]
    ctx = orch.context(s)
    with pytest.raises(ActionError):
        await orch.actions.confirm(ctx, pending.action_id, None, "test")
    with pytest.raises(ActionError):
        await orch.actions.confirm(ctx, pending.action_id, "000000" if pending.demo_otp != "000000" else "111111", "test")
    first = await orch.actions.confirm(ctx, pending.action_id, pending.demo_otp, "test")
    again = await orch.actions.confirm(ctx, pending.action_id, pending.demo_otp, "test")
    assert first == again and first["request_id"].startswith("SR")
    duplicate, _ = await call(orch, s, "request_prepayment", loan_id="LN10001", amount=50000)
    assert duplicate["status"] == "already_pending"


async def test_dispute_reverses_eligible_charge(orch):
    s = session("CUST1002")
    proposal, _ = await call(orch, s, "raise_charge_dispute", loan_id="LN10002", charge_id="CHG10002020A",
                             customer_reason="bank error")
    result = await orch.actions.confirm(orch.context(s), proposal["action_id"], None, "test")
    assert result["status"] == "RESOLVED" and result["reversal"]["amount"] == "₹590"
    history, _ = await call(orch, s, "get_payment_history", failed_only=True)
    assert history["summary"]["charges_eligible_for_reversal"] == []


async def test_card_workflow_reuses_the_platform(orch):
    s = session("CUST1003", "card_servicing")
    txns, _ = await call(orch, s, "get_card_transactions", card_id="CC1003", status="declined")
    assert txns["transactions"][0]["decline"]["reason"] == "International usage turned off"
    quote, _ = await call(orch, s, "simulate_card_emi", card_id="CC1003", txn_id="TXA900")
    assert quote["eligible"] and len(quote["options"]) == 4
    proposal, _ = await call(orch, s, "set_international_usage", card_id="CC1003", enabled=True)
    assert proposal["requires_otp"]
