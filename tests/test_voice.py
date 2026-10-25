"""Voice: text written for the screen becomes text a voice can say; interruptions keep history honest."""

import asyncio

import httpx
import pytest

from app.platform.actions import ActionManager, AuditLog
from app.platform.connectors import Connectors
from app.platform.llm.base import Round, usage_dict
from app.platform.orchestrator import Orchestrator
from app.platform.policy import PolicyEngine
from app.platform.session import Session
from app.platform.telemetry import MetricsStore
from app.platform.usecase import load_usecases
from app.platform.voice import speakable
from app.settings import settings
from finlib.dates import business_date
from mock_systems import app as mock_app


def test_amounts_dates_and_markup_are_spoken_naturally():
    assert speakable("Your EMI went from ₹20,969 to ₹21,179 on **05 Oct 2026** [RR-1][RR-2].") == \
        "Your EMI went from 20 thousand 969 rupees to 21 thousand 179 rupees on 5 October 2026."
    assert speakable("Pay ₹1,85,064.28 today; about ₹64.70 a day.") == \
        "Pay 1 lakh 85 thousand 64 rupees today; about 64 rupees 70 paise a day."
    assert speakable("- Rate moved from 11.75 % to 12.50 % p.a.") == "Rate moved from 11.75 percent to 12.5 percent a year."
    assert speakable("| Option | Saved |\n|---|---|\n| Keep EMI | ₹18,828.95 |") == "Option, Saved. Keep EMI, 18 thousand 829 rupees."


class InterruptibleModel:
    """Starts answering, then hangs as if the model were still generating."""
    name, model = "scripted", "scripted-1"

    def user_messages(self, content):
        return [{"role": "user", "content": content}]

    def assistant_messages(self, text):
        return [{"role": "assistant", "content": text}]

    def tool_result_messages(self, outcomes):
        return []

    async def stream_round(self, system, messages, tools, effort, on_text, on_tool_start):
        assert "live voice conversation" in system
        await on_text("Your next EMI is ")
        await asyncio.sleep(30)
        return Round([], [], "end", self.model, usage_dict())

    def friendly_error(self, exc):
        return None


@pytest.mark.asyncio
async def test_barge_in_keeps_only_what_was_said(tmp_path):
    connectors = Connectors("http://core", settings.systems_config, transport=httpx.ASGITransport(app=mock_app.app))
    orch = Orchestrator(settings, connectors, PolicyEngine(connectors), ActionManager(AuditLog(tmp_path / "a.jsonl")),
                        MetricsStore(tmp_path / "t.jsonl"), load_usecases(settings.config_dir / "usecases"),
                        business_date, llm=InterruptibleModel())
    session = Session("s", "CUST1001", "Priya", ["loan_servicing"])

    async def emit(_):
        pass

    task = asyncio.create_task(orch.run_turn(session, "What's my next EMI?", emit, channel="voice"))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await connectors.close()
    assert session.messages == [
        {"role": "user", "content": "What's my next EMI?"},
        {"role": "assistant", "content": "Your next EMI is  [reply cut off: the customer interrupted]"},
    ]
    assert not session.lock.locked()
    assert orch.metrics.turns[-1]["error"] == "cancelled"


def test_noise_and_echo_cannot_interrupt():
    from app.platform.voice import is_echo, is_noise
    assert is_noise("", 0.0) and is_noise(" Thank you.", 0.1) and is_noise("Check my payments", 0.9)
    assert not is_noise("Actually, check my last payment first.", 0.05)
    assert is_noise("EMI, prepayment, foreclosure, NACH.", 0.2)        # Whisper repeating its vocabulary hint
    assert is_noise("Check my payment", 0.4, -1.2)                       # unsure and probably silence
    assert not is_noise("What about prepayment?", 0.1, -0.3)
    heard = "Your next EMI of ₹21,179 is due on 5 November 2026. [cut off]"
    assert is_echo("Your next EMI is due on 5 November", heard)
    assert not is_echo("Actually, check my last payment first.", heard)
