"""Business rules against the real policy documents, with fixed loan data."""

import asyncio
from datetime import date
from decimal import Decimal

from app.domains.lending.rules import evaluate_prepayment, foreclosure_quote, reversal_eligibility
from app.platform.policy import PolicyDocument, Rule
from mock_systems.app import policy_document

TODAY = date(2026, 10, 25)


def load(family: str, on: date = TODAY) -> PolicyDocument:
    data = asyncio.run(policy_document(family, on.isoformat()))
    return PolicyDocument(family, data["title"], data["version"], data["resolved_on"],
                          {r["id"]: Rule(r["id"], r["title"], r["text"], r.get("params", {}), r.get("tags", []),
                                         r["version"], r["effective_from"]) for r in data["rules"]},
                          data["reference"])


def loan(**overrides) -> dict:
    base = {"rate_type": "fixed", "outstanding_principal": "174923.91", "current_rate": "13.50", "emi": "13574",
            "emis_paid": 22, "emis_remaining": 14, "first_due_date": "2025-01-05", "next_due_date": "2026-11-05",
            "last_emi_due_date": "2026-10-05", "disbursal_date": "2024-12-15"}
    return base | overrides


def test_policy_versions_resolve_by_date():
    assert load("personal_loan", date(2026, 3, 31)).rule("PP-2").params["min_amount"] == 25000
    assert load("personal_loan", date(2026, 4, 1)).rule("PP-2").params["min_amount"] == 10000


def test_floating_prepayment_has_no_charge():
    ev = evaluate_prepayment(loan(rate_type="floating", current_rate="12.50"), 50000, load("personal_loan"), 0)
    assert ev.eligible and ev.total_charges == 0


def test_fixed_prepayment_charge_and_gst():
    ev = evaluate_prepayment(loan(), 20000, load("personal_loan"), 0)
    assert ev.charge == Decimal("600.00") and ev.gst == Decimal("108.00")


def test_lock_in_blocks_prepayment():
    ev = evaluate_prepayment(loan(emis_paid=4, first_due_date="2026-07-05"), 30000, load("personal_loan"), 0)
    assert not ev.eligible
    assert "05 Dec 2026" in ev.blockers[0]


def test_minimum_amount_and_full_payoff_are_blocked():
    pol = load("personal_loan")
    assert not evaluate_prepayment(loan(), 5000, pol, 0).eligible
    assert not evaluate_prepayment(loan(), 200000, pol, 0).eligible


def test_fixed_foreclosure_slab_and_waiting_option():
    upcoming = [{"due_date": "2026-11-05", "closing": "163317.11", "interest": "1967.89"},
                {"due_date": "2026-12-05", "closing": "151580.03", "interest": "1837.32"},
                {"due_date": "2027-01-05", "closing": "139711.00", "interest": "1705.27"}]
    q = foreclosure_quote(loan(), load("personal_loan"), TODAY, Decimal("590"), upcoming)
    assert q.charge_pct == Decimal("4.0") and not q.penalty_free
    assert q.next_slab["from_date"] == date(2026, 12, 15)
    assert q.next_slab["emis_before"] == 2
    assert q.total == q.outstanding + q.accrued_interest + Decimal("590") + q.charge + q.gst


def test_floating_foreclosure_is_penalty_free():
    q = foreclosure_quote(loan(rate_type="floating"), load("personal_loan"), TODAY, Decimal(0), [])
    assert q.penalty_free and q.charge == 0 and q.next_slab is None


def test_bounce_charge_reversal_only_when_not_customers_fault():
    pol = load("personal_loan")
    charge = {"status": "COLLECTED", "charge_date": "2026-08-05"}
    assert reversal_eligibility("TECHNICAL_DECLINE", charge, pol, TODAY)[0]
    assert not reversal_eligibility("INSUFFICIENT_FUNDS", charge, pol, TODAY)[0]
    assert not reversal_eligibility("TECHNICAL_DECLINE", {**charge, "charge_date": "2026-05-05"}, pol, TODAY)[0]
    assert not reversal_eligibility("TECHNICAL_DECLINE", {**charge, "status": "REVERSED"}, pol, TODAY)[0]
