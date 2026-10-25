"""Loan servicing tools: account data, explanations, simulations and actions."""

import asyncio
from collections import defaultdict
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from finlib import amortization as am
from finlib.dates import add_months, financial_year_start, fmt
from finlib.money import inr, pct

from ...platform.actions import action
from ...platform.tools import ToolContext, ToolError, tool
from ...platform.usecase import context_provider, panel
from ..common.tools import log_interaction, profile, request_brief, sla_days
from .rules import D, evaluate_prepayment, foreclosure_quote, iso, reversal_eligibility

FAMILY = "personal_loan"


def f(value: str | None) -> str | None:
    return fmt(iso(value)) if value else None


# ---------------------------------------------------------------------------
# Data access helpers
# ---------------------------------------------------------------------------

async def customer_loans(ctx: ToolContext) -> list[dict]:
    loans = await ctx.connectors["loans"].get(f"/loans/customers/{ctx.customer_id}")
    ctx.evidence.data("Loan Management", "Loan accounts")
    return loans


async def owned_loan(ctx: ToolContext, loan_id: str) -> dict:
    """Only loans on the signed-in customer's profile are reachable, whatever ID the model passes."""
    loan_id = loan_id.strip().upper()
    for loan in await customer_loans(ctx):
        if loan["loan_id"] == loan_id:
            return loan
    raise ToolError(f"No loan {loan_id} on this customer's profile. Use list_loans to see their loans.")


async def policy(ctx: ToolContext):
    doc = await ctx.policy.document(FAMILY, ctx.today)
    ctx.evidence.data("Policy Knowledge", f"{doc.title} (v{doc.version})")
    return doc


async def prepayments_this_fy(ctx: ToolContext, loan_id: str) -> tuple[int, list[dict]]:
    requests = await ctx.connectors["service_ops"].get(f"/service-ops/customers/{ctx.customer_id}/requests",
                                                       {"status": "all"})
    fy_start = financial_year_start(ctx.today).isoformat()
    mine = [r for r in requests if r["type"] == "PREPAYMENT" and r["product_ref"] == loan_id]
    return sum(1 for r in mine if r["created_at"] >= fy_start), mine


def rate_text(loan: dict) -> str:
    if loan["rate_type"] == "floating":
        benchmark = D(loan["current_rate"]) - D(loan["spread"])
        return f"{loan['current_rate']}% a year, floating (NWBR {benchmark}% + {loan['spread']}% spread)"
    return f"{loan['current_rate']}% a year, fixed"


def loan_brief(loan: dict) -> dict:
    brief = {
        "loan_id": loan["loan_id"], "product": loan["product_name"], "status": loan["status"],
        "interest_rate": rate_text(loan), "loan_amount": inr(loan["principal"]),
        "disbursed_on": f(loan["disbursal_date"]), "original_tenure_months": loan["tenure_months"],
        "current_emi": inr(loan["emi"]), "outstanding_principal": inr(loan["outstanding_principal"]),
        "emis_paid": loan["emis_paid"], "emis_remaining": loan["emis_remaining"],
        "next_emi": {"due_on": f(loan["next_due_date"]), "amount": inr(loan["next_emi_amount"])}
        if loan["next_due_date"] else None,
        "scheduled_last_emi": f(loan["maturity_date"]),
        "auto_debit_mandate": (f"Active ({loan['mandate_bank']})" if loan["mandate_status"] == "ACTIVE" else
                               f"INACTIVE since {f(loan['mandate_since'])}. EMIs must be paid directly until a new "
                               f"mandate is registered [MD-1]."),
    }
    if D(loan["overdue_amount"]) > 0:
        brief["unpaid_charges"] = inr(loan["overdue_amount"])
    return brief


async def rate_outlook(ctx: ToolContext, loan: dict) -> dict | None:
    """For floating loans: when the next reset is and whether the benchmark has moved since the last one."""
    if loan["rate_type"] != "floating":
        return None
    benchmarks = await ctx.connectors["loans"].get("/loans/benchmarks")
    latest = max(benchmarks, key=lambda b: b["effective_from"])
    next_reset = iso(loan["disbursal_date"])
    while next_reset <= ctx.today:
        next_reset = add_months(next_reset, 3)
    in_use = D(loan["current_rate"]) - D(loan["spread"])
    outlook = {"next_reset_on": fmt(next_reset), "nwbr_now": f"{latest['rate']}% (since {f(latest['effective_from'])})",
               "nwbr_in_your_rate": f"{in_use}%"}
    if D(latest["rate"]) != in_use:
        expected = D(latest["rate"]) + D(loan["spread"])
        outlook["expected_change"] = (f"Unless NWBR moves again, the rate should change from {loan['current_rate']}% "
                                      f"to {expected}% at the reset on {fmt(next_reset)}, revising the EMI from the "
                                      f"next EMI after that [RR-1][RR-2].")
    return outlook


# ---------------------------------------------------------------------------
# Context and panels
# ---------------------------------------------------------------------------

@context_provider("loans")
async def loans_context(ctx: ToolContext) -> str:
    loans = await customer_loans(ctx)
    if not loans:
        return "Loans: none."
    parts = [f"{l['loan_id']} {l['product_name']} ({l['rate_type']} rate, EMI due on the 5th)" for l in loans]
    return "Loans: " + "; ".join(parts) + "."


@panel("loans")
async def loans_panel(ctx: ToolContext) -> dict:
    loans = await customer_loans(ctx)
    return {"items": [{
        "loan_id": l["loan_id"], "product": l["product_name"], "rate_type": l["rate_type"],
        "rate": f"{l['current_rate']}%", "outstanding": inr(l["outstanding_principal"]), "emi": inr(l["emi"]),
        "next_due": f(l["next_due_date"]), "emis_paid": l["emis_paid"],
        "emis_total": l["emis_paid"] + l["emis_remaining"], "mandate_status": l["mandate_status"],
        "unpaid_charges": inr(l["overdue_amount"]) if D(l["overdue_amount"]) > 0 else None} for l in loans]}


@panel("loan_payments")
async def payments_panel(ctx: ToolContext) -> dict:
    loans = await customer_loans(ctx)
    since = add_months(ctx.today, -6).isoformat()
    batches = await asyncio.gather(*(ctx.connectors["payments"].get(f"/payments/loans/{l['loan_id']}/payments",
                                                                     {"since": since}) for l in loans))
    attempts = sorted((p for batch in batches for p in batch), key=lambda p: (p["attempt_date"], p["payment_id"]),
                      reverse=True)
    return {"items": [{"loan_id": p["loan_id"], "date": f(p["attempt_date"]), "amount": inr(p["amount"]),
                       "method": p["method"], "status": p["status"], "reason": p["reason_code"]} for p in attempts[:8]]}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

class LoanArgs(BaseModel):
    loan_id: str = Field(..., description="e.g. LN10001")


class LoansArgs(BaseModel):
    loan_id: str | None = Field(None, description="Omit for all loans")


@tool("get_loans", "The customer's loans (or one): rate, EMI, outstanding, next EMI, mandate, next rate reset.",
      label="Fetching your loans", input_model=LoansArgs)
async def get_loans(ctx: ToolContext, args: LoansArgs) -> dict:
    loans = [await owned_loan(ctx, args.loan_id)] if args.loan_id else await customer_loans(ctx)
    outlooks = await asyncio.gather(*(rate_outlook(ctx, l) for l in loans))
    briefs = [loan_brief(l) | ({"rate_outlook": o} if o else {}) for l, o in zip(loans, outlooks)]
    result = {"loans": briefs, "as_of": fmt(ctx.today)}
    if len(loans) > 1:  # customers ask for totals; give one so the model never adds figures itself
        result["total_of_next_emis"] = inr(sum((D(l["next_emi_amount"]) for l in loans if l["next_emi_amount"]), Decimal(0)))
    return result


class ScheduleArgs(BaseModel):
    loan_id: str
    which: Literal["upcoming", "past"] = "upcoming"
    count: int = Field(6, ge=1, le=24)


@tool("get_repayment_schedule", "Upcoming or past EMIs of a loan with interest/principal split and balance.",
      label="Fetching your repayment schedule", input_model=ScheduleArgs)
async def get_repayment_schedule(ctx: ToolContext, args: ScheduleArgs) -> dict:
    loan = await owned_loan(ctx, args.loan_id)
    status = "UPCOMING" if args.which == "upcoming" else "PAID"
    rows = await ctx.connectors["loans"].get(f"/loans/accounts/{loan['loan_id']}/schedule", {"status": status})
    rows = rows[:args.count] if args.which == "upcoming" else rows[-args.count:]
    ctx.evidence.data("Loan Management", f"Repayment schedule {loan['loan_id']}")
    return {"loan_id": loan["loan_id"], "instalments": [{
        "no": r["inst_no"], "due_on": f(r["due_date"]), "emi": inr(r["emi"]), "interest": inr(r["interest"]),
        "principal": inr(r["principal"]), "balance_after": inr(r["closing"]), "rate": f"{r['rate']}%",
        **({"paid_on": f(r["paid_date"])} if r["paid_date"] else {})} for r in rows]}


@tool("explain_emi_change", "Why a loan's EMI changed (or confirm it didn't): cause, recomputed EMI, notice dates, "
      "tenure-extension alternative.", label="Analysing your EMI history", input_model=LoanArgs)
async def explain_emi_change(ctx: ToolContext, args: LoanArgs) -> dict:
    loan = await owned_loan(ctx, args.loan_id)
    lid = loan["loan_id"]
    paid, events, notices, pol, outlook = await asyncio.gather(
        ctx.connectors["loans"].get(f"/loans/accounts/{lid}/schedule", {"status": "PAID"}),
        ctx.connectors["loans"].get(f"/loans/accounts/{lid}/events"),
        ctx.connectors["crm"].get(f"/crm/customers/{ctx.customer_id}/interactions", {"limit": 30}),
        policy(ctx), rate_outlook(ctx, loan))
    ctx.evidence.data("Loan Management", f"EMI history and loan events {lid}")
    if len(paid) < 2:
        return {"loan_id": lid, "changed": False, "note": "Only one EMI has fallen due so far.",
                "current_emi": inr(loan["emi"])}

    change_at = next((i for i in range(len(paid) - 1, 0, -1) if paid[i]["emi"] != paid[i - 1]["emi"]), None)
    if change_at is None:
        result = {"loan_id": lid, "changed": False, "current_emi": inr(paid[-1]["emi"]),
                  "unchanged_since": f(paid[0]["due_date"]), "emis_checked": len(paid)}
        if outlook:
            result["rate_outlook"] = outlook
            ctx.evidence.rules(pol, "RR-1", "RR-2")
        return result

    changed, before = paid[change_at], paid[change_at - 1]
    diff = D(changed["emi"]) - D(before["emi"])
    result = {
        "loan_id": lid, "changed": True,
        "latest_emi": {"due_on": f(paid[-1]["due_date"]), "amount": inr(paid[-1]["emi"])},
        "change": {"from": inr(before["emi"]), "to": inr(changed["emi"]),
                   "difference": ("+" if diff > 0 else "") + inr(diff), "percent": f"{pct(diff, before['emi'])}%",
                   "first_changed_emi_due_on": f(changed["due_date"]),
                   "changed_in_latest_emi": change_at == len(paid) - 1},
    }
    reset = next((e for e in events if e["type"] == "RATE_RESET"
                  and e["details"]["first_revised_instalment"] == changed["inst_no"]), None)
    if reset is None:
        result["cause"] = {"type": "Not found in the loan record",
                           "guidance": "Say the record doesn't show the reason and offer a GENERAL service request."}
        return result

    det = reset["details"]
    outstanding, new_rate = D(det["outstanding_at_reset"]), D(det["new_rate"])
    old_emi, new_emi = D(det["old_emi"]), D(det["new_emi"])
    first_due = iso(det["first_revised_due_date"])
    recomputed = am.emi(outstanding, new_rate, det["remaining_instalments"])
    revised_rows = am.schedule(outstanding, new_rate, new_emi, first_due, max_months=det["remaining_instalments"])
    keep_rows = am.schedule(outstanding, new_rate, old_emi, first_due)
    rr3 = pol.rule("RR-3").params
    total_tenure = changed["inst_no"] - 1 + len(keep_rows)
    result["cause"] = {
        "type": "Floating rate reset",
        "reset_date": f(det["reset_date"]),
        "benchmark_change": f"NWBR rose from {det['old_benchmark_rate']}% to {det['new_benchmark_rate']}%",
        "your_rate": f"{det['old_rate']}% to {det['new_rate']}% (your {det['spread']}% spread is unchanged)",
        "outstanding_principal_at_reset": inr(outstanding),
        "emis_remaining_at_reset": det["remaining_instalments"],
        "how_the_new_emi_was_set": f"Recalculated on the outstanding principal so the loan still ends on "
                                   f"{f(loan['maturity_date'])} [RR-1][RR-2]",
        "recalculation_check": f"{inr(recomputed)} recomputed independently, "
                               f"{'matches' if recomputed == new_emi else 'does NOT match'} the loan system",
    }
    result["alternative"] = {
        "option": f"Keep the EMI at {inr(old_emi)} and extend the tenure instead [RR-3]",
        "extra_emis": len(keep_rows) - len(revised_rows),
        "last_emi_would_be": fmt(keep_rows[-1].due_date),
        "extra_interest_vs_current_plan": inr(am.total_interest(keep_rows) - am.total_interest(revised_rows)),
        "allowed": total_tenure <= rr3["max_total_tenure_months"],
        "how": f"TENURE_CHANGE service request, processed in {rr3['sla_working_days']} working days",
    }
    notified = [{"on": f(n["ts"]), "channel": n["channel"]} for n in notices if n["type"] == "RATE_REVISION_NOTICE"]
    if notified:
        result["customer_notified"] = notified
        ctx.evidence.data("Customer CRM", "Rate revision notices")
    if outlook:
        result["rate_outlook"] = outlook
    ctx.evidence.rules(pol, "RR-1", "RR-2", "RR-3")
    ctx.evidence.calculation(
        f"Revised EMI at the {f(det['reset_date'])} reset ({lid})",
        {"Outstanding principal": inr(outstanding), "New rate": f"{new_rate}% a year",
         "EMIs remaining": det["remaining_instalments"]},
        {"Revised EMI": inr(recomputed), "Previous EMI": inr(old_emi), "Loan system EMI": inr(new_emi)},
        "EMI = P x r x (1+r)^n / ((1+r)^n - 1), r = annual rate / 12, rounded to the rupee")
    ctx.evidence.calculation(
        "Alternative: keep the old EMI and extend the tenure",
        {"Outstanding principal": inr(outstanding), "Rate": f"{new_rate}% a year", "EMI kept at": inr(old_emi)},
        {"EMIs needed": len(keep_rows), "Extra EMIs": len(keep_rows) - len(revised_rows),
         "Extra interest": inr(am.total_interest(keep_rows) - am.total_interest(revised_rows))},
        "Month-by-month reducing balance projection at the revised rate")
    return result


class PaymentArgs(BaseModel):
    loan_id: str | None = Field(None, description="Omit for all loans")
    months: int = Field(6, ge=1, le=24)
    failed_only: bool = False


@tool("get_payment_history", "EMI payments: failed auto-debits with plain reasons, how each was paid, bounce charges "
      "and whether they can be reversed.", label="Checking your payment history", input_model=PaymentArgs)
async def get_payment_history(ctx: ToolContext, args: PaymentArgs) -> dict:
    loans = [await owned_loan(ctx, args.loan_id)] if args.loan_id else await customer_loans(ctx)
    since = add_months(ctx.today, -args.months)
    pol = await policy(ctx)
    fetched = await asyncio.gather(*(asyncio.gather(
        ctx.connectors["payments"].get(f"/payments/loans/{l['loan_id']}/payments", {"since": since.isoformat()}),
        ctx.connectors["payments"].get(f"/payments/loans/{l['loan_id']}/charges")) for l in loans))
    ctx.evidence.data("Payments", "Payment attempts and charges")
    codes = pol.reference.get("payment_reason_codes", {})
    entries, failed_count = [], 0
    charges_total, unpaid_total, reversible = Decimal(0), Decimal(0), []
    for loan, (payments, charges) in zip(loans, fetched):
        charge_by_payment = {c["payment_id"]: c for c in charges}
        by_inst = defaultdict(list)
        for p in payments:
            by_inst[p["inst_no"]].append(p)
        for inst, attempts in sorted(by_inst.items()):
            failed = [a for a in attempts if a["status"] == "FAILED"]
            ok = next((a for a in attempts if a["status"] == "SUCCESS"), None)
            if args.failed_only and not failed:
                continue
            due = iso(attempts[0]["due_date"])
            entry = {"loan_id": loan["loan_id"], "instalment": inst, "due_on": fmt(due),
                     "amount": inr(attempts[0]["amount"])}
            if ok:
                late = (iso(ok["attempt_date"]) - due).days
                entry.update(status="Paid late" if late > 0 else "Paid", paid_on=f(ok["attempt_date"]),
                             paid_via=ok["method"], **({"days_late": late} if late > 0 else {}))
            else:
                entry["status"] = "Unpaid"
            for a in failed:
                failed_count += 1
                ref = codes.get(a["reason_code"], {})
                entry["failed_attempt"] = {"on": f(a["attempt_date"]), "method": "Auto-debit (NACH)",
                                           "reason": ref.get("label", a["reason_code"]),
                                           "what_happened": ref.get("explanation"),
                                           "caused_by_customer": ref.get("customer_attributable"),
                                           "advice": ref.get("advice")}
                charge = charge_by_payment.get(a["payment_id"])
                if charge:
                    charges_total += D(charge["total"])
                    status = {"COLLECTED": f"Collected on {f(charge['settled_date'])}", "OUTSTANDING": "Unpaid",
                              "REVERSED": f"Reversed on {f(charge['settled_date'])}"}[charge["status"]]
                    if charge["status"] == "OUTSTANDING":
                        unpaid_total += D(charge["total"])
                    eligible, why = reversal_eligibility(a["reason_code"], charge, pol, ctx.today)
                    entry["bounce_charge"] = {"charge_id": charge["charge_id"],
                                              "amount": f"{inr(charge['total'])} ({inr(charge['amount'])} + GST)",
                                              "status": status, "rule": f"BC-1 v{charge['policy_version']}",
                                              "reversal_eligible": eligible, "reversal_note": why}
                    if eligible:
                        reversible.append(charge["charge_id"])
            entries.append(entry)
    result = {"period": f"{fmt(since)} to {fmt(ctx.today)}", "instalments": entries,
              "summary": {"failed_auto_debits": failed_count, "bounce_charges": inr(charges_total),
                          "unpaid_charges": inr(unpaid_total), "charges_eligible_for_reversal": reversible}}
    inactive = [l for l in loans if l["mandate_status"] != "ACTIVE"]
    if inactive:
        result["mandate_warning"] = [f"{l['loan_id']}: auto-debit mandate inactive since {f(l['mandate_since'])}. "
                                     f"The next EMI of {inr(l['next_emi_amount'])} on {f(l['next_due_date'])} must be "
                                     f"paid directly or a new mandate registered [MD-1]." for l in inactive]
    ctx.evidence.rules(pol, "BC-1", "BC-2", "BC-3", *(["MD-1"] if inactive else []))
    return result


class PrepayArgs(BaseModel):
    loan_id: str
    amount: float = Field(..., gt=0, description="Rupees")


@tool("simulate_prepayment", "Any 'what if I prepay / pay extra' question: eligibility, charges, shorter-tenure and "
      "lower-EMI options with exact interest saved.", label="Calculating the prepayment impact", input_model=PrepayArgs)
async def simulate_prepayment(ctx: ToolContext, args: PrepayArgs) -> dict:
    loan = await owned_loan(ctx, args.loan_id)
    pol, (count_fy, _) = await asyncio.gather(policy(ctx), prepayments_this_fy(ctx, loan["loan_id"]))
    ev = evaluate_prepayment(loan, args.amount, pol, count_fy)
    ctx.evidence.rules(pol, "PP-1", "PP-2", "PP-3", "PP-4", "PP-5")
    result = {"loan_id": loan["loan_id"], "prepayment": inr(ev.amount), "eligible_today": ev.eligible,
              "blockers": ev.blockers,
              "charges": {"amount": inr(ev.total_charges), "basis": ev.charge_basis}}
    sim = ev.simulation
    if sim:
        base, tenure, emi_opt = sim["baseline"], sim["reduce_tenure"], sim["reduce_emi"]
        result["current_plan"] = {"emi": inr(base["emi"]), "emis_left": base["instalments_left"],
                                  "last_emi_on": fmt(base["last_emi_date"]),
                                  "interest_still_to_pay": inr(base["interest_remaining"])}
        result["option_keep_emi_shorten_tenure"] = {
            "emi": inr(tenure["emi"]), "emis_left": tenure["instalments_left"],
            "fewer_emis": tenure["instalments_saved"], "last_emi_on": fmt(tenure["last_emi_date"]),
            "interest_saved": inr(tenure["interest_saved"]),
            "net_saving_after_charges": inr(tenure["interest_saved"] - ev.total_charges)}
        result["option_keep_tenure_lower_emi"] = {
            "new_emi": inr(emi_opt["emi"]), "emi_lower_by": inr(emi_opt["emi_reduction"]),
            "emis_left": emi_opt["instalments_left"], "last_emi_on": fmt(emi_opt["last_emi_date"]),
            "interest_saved": inr(emi_opt["interest_saved"]),
            "net_saving_after_charges": inr(emi_opt["interest_saved"] - ev.total_charges)}
        result["default_option"] = "Keep EMI, shorten tenure [PP-5]"
        result["assumptions"] = [f"Prepayment is credited before the next EMI on {f(loan['next_due_date'])}.",
                                 f"Interest stays at {loan['current_rate']}%"
                                 + (" (floating rates can change at the next reset)." if loan["rate_type"] == "floating" else ".")]
        if not ev.eligible:
            result["note"] = "Figures are illustrative until the blockers are cleared."
        ctx.evidence.calculation(
            f"Part-prepayment of {inr(ev.amount)} on {loan['loan_id']}",
            {"Outstanding principal": inr(loan["outstanding_principal"]), "Rate": f"{loan['current_rate']}% a year",
             "Current EMI": inr(loan["emi"]), "EMIs left": loan["emis_remaining"], "Prepayment": inr(ev.amount)},
            {"Interest saved (shorter tenure)": inr(tenure["interest_saved"]), "Fewer EMIs": tenure["instalments_saved"],
             "Interest saved (lower EMI)": inr(emi_opt["interest_saved"]), "New EMI (lower EMI option)": inr(emi_opt["emi"]),
             "Charges": inr(ev.total_charges)},
            "Both plans projected month by month on a reducing balance; interest saved = interest left on the current "
            "plan minus interest left after prepayment", result["assumptions"])
    return result


@tool("check_foreclosure", "Closing the loan early: eligibility, penalty-free or not, exact payoff with breakdown, "
      "and whether waiting for a lower charge saves money.", label="Calculating your foreclosure amount",
      input_model=LoanArgs)
async def check_foreclosure(ctx: ToolContext, args: LoanArgs) -> dict:
    loan = await owned_loan(ctx, args.loan_id)
    lid = loan["loan_id"]
    pol, charges, upcoming = await asyncio.gather(
        policy(ctx), ctx.connectors["payments"].get(f"/payments/loans/{lid}/charges"),
        ctx.connectors["loans"].get(f"/loans/accounts/{lid}/schedule", {"status": "UPCOMING"}))
    unpaid = sum((D(c["total"]) for c in charges if c["status"] == "OUTSTANDING"), Decimal(0))
    q = foreclosure_quote(loan, pol, ctx.today, unpaid, upcoming)
    ctx.evidence.rules(pol, "FC-1", "FC-2", "FC-3", *(["BC-1"] if unpaid else []))
    result = {
        "loan_id": lid, "rate_type": loan["rate_type"], "eligible_today": q.eligible, "blockers": q.blockers,
        "penalty_free": q.penalty_free,
        "foreclosure_charge_rule": ("No foreclosure charge on floating rate loans [FC-2]" if loan["rate_type"] == "floating"
                                    else f"{q.charge_pct}% of outstanding principal + GST, as the loan is "
                                         f"{q.months_since_disbursal} months old [FC-2]"),
        "amount_to_close_today": inr(q.total),
        "breakdown": {"Outstanding principal": inr(q.outstanding),
                      f"Interest since {f(loan['last_emi_due_date'])}": inr(q.accrued_interest),
                      "Unpaid charges": inr(q.unpaid_charges), "Foreclosure charge": inr(q.charge),
                      "GST on charge": inr(q.gst)},
        "quote_valid_for": f"{fmt(ctx.today)}; interest adds about {inr(q.per_day_interest)} a day",
    }
    if q.next_slab:
        s = q.next_slab
        result["waiting_option"] = {
            "lower_charge_from": fmt(s["from_date"]), "charge_rate_then": f"{s['pct']}%",
            "emis_due_before_then": s["emis_before"], "charge_with_gst_then": inr(s["charge_then"]),
            "charge_saving": inr(s["charge_saving"]), "interest_paid_in_those_emis": inr(s["interest_meanwhile"]),
            "net_benefit_of_waiting": inr(s["net_benefit"]),
            "verdict": "Waiting saves money overall" if s["net_benefit"] > 0 else "Waiting does not save money overall"}
    ctx.evidence.calculation(
        f"Foreclosure quote for {lid} on {fmt(ctx.today)}",
        {"Outstanding principal": inr(q.outstanding), "Rate": f"{loan['current_rate']}% a year",
         "Days since last EMI": (ctx.today - iso(loan["last_emi_due_date"])).days if loan["last_emi_due_date"] else 0,
         "Charge rate": f"{q.charge_pct}%"},
        {"Accrued interest": inr(q.accrued_interest), "Foreclosure charge + GST": inr(q.charge + q.gst),
         "Unpaid charges": inr(q.unpaid_charges), "Total to close": inr(q.total)},
        "Principal + interest accrued (actual days / 365) + unpaid charges + foreclosure charge + 18% GST [FC-3]")
    return result


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

class PrepayRequestArgs(BaseModel):
    loan_id: str
    amount: float = Field(..., gt=0)
    option: Literal["reduce_tenure", "reduce_emi"] = "reduce_tenure"


@tool("request_prepayment", "Prepare a part-prepayment request once the customer decides (they confirm with OTP).",
      label="Preparing your prepayment request", input_model=PrepayRequestArgs, kind="action")
async def request_prepayment(ctx: ToolContext, args: PrepayRequestArgs) -> dict:
    loan = await owned_loan(ctx, args.loan_id)
    pol, (count_fy, existing), who = await asyncio.gather(policy(ctx), prepayments_this_fy(ctx, loan["loan_id"]),
                                                          profile(ctx))
    ev = evaluate_prepayment(loan, args.amount, pol, count_fy)
    if not ev.eligible:
        return {"status": "not_allowed", "blockers": ev.blockers}
    pending = [r for r in existing if r["status"] not in ("CLOSED", "RESOLVED", "CANCELLED")]
    if pending:
        return {"status": "already_pending", "request": request_brief(pending[0]),
                "note": "Only one prepayment request can be open per loan."}
    option = ev.simulation["reduce_tenure" if args.option == "reduce_tenure" else "reduce_emi"]
    effect = (f"EMI stays {inr(option['emi'])}; {option['instalments_saved']} fewer EMIs, last EMI on "
              f"{fmt(option['last_emi_date'])}" if args.option == "reduce_tenure" else
              f"EMI drops to {inr(option['emi'])} from the next EMI; last EMI stays on {fmt(option['last_emi_date'])}")
    days = await sla_days(ctx, "PREPAYMENT")
    proposal = await ctx.actions.propose(ctx, "prepayment_request", params={
        "loan_id": loan["loan_id"], "amount": str(ev.amount), "option": args.option,
        "charges": str(ev.total_charges), "interest_saved": str(option["interest_saved"]), "sla_working_days": days},
        summary=[{"label": "Loan", "value": f"{loan['loan_id']} ({loan['product_name']})"},
                 {"label": "Prepayment", "value": inr(ev.amount)},
                 {"label": "Effect", "value": effect},
                 {"label": "Interest saved", "value": inr(option["interest_saved"])},
                 {"label": "Charges", "value": inr(ev.total_charges)},
                 {"label": "Payment", "value": "Secure payment link by SMS once you confirm"}],
        otp_destination=who["mobile"])
    ctx.evidence.rules(pol, "PP-1", "PP-2", "PP-4", "PP-5")
    return proposal.for_model()


@action("prepayment_request", "Part-prepayment request", requires_otp=True)
async def execute_prepayment(ctx: ToolContext, params: dict, key: str) -> dict:
    amount = inr(params["amount"])
    created = await ctx.connectors["service_ops"].post("/service-ops/requests", {
        "customer_id": ctx.customer_id, "product_ref": params["loan_id"], "type": "PREPAYMENT",
        "summary": f"Part-prepayment of {amount} ({params['option'].replace('_', ' ')})", "details": params,
        "sla_working_days": params["sla_working_days"], "channel": f"assistant-{ctx.channel}"},
        idempotency_key=f"{key}:sr")
    await log_interaction(ctx, "SERVICE_REQUEST", f"Prepayment request {created['request_id']} for {amount}", key)
    return {"request_id": created["request_id"], "status": created["status"],
            "due_by": fmt(iso(created["sla_due"])),
            "next_step": f"A secure payment link for {amount} is sent to the registered mobile. The prepayment is "
                         f"applied within {params['sla_working_days']} working days of payment and the revised "
                         f"schedule is emailed."}


class DisputeArgs(BaseModel):
    loan_id: str
    charge_id: str = Field(..., description="From get_payment_history")
    customer_reason: str


@tool("raise_charge_dispute", "Prepare a dispute of a bounce charge (customer confirms). Reversed at once if policy "
      "allows, otherwise reviewed.", label="Preparing your dispute", input_model=DisputeArgs, kind="action")
async def raise_charge_dispute(ctx: ToolContext, args: DisputeArgs) -> dict:
    loan = await owned_loan(ctx, args.loan_id)
    lid = loan["loan_id"]
    pol, charges, payments, requests = await asyncio.gather(
        policy(ctx), ctx.connectors["payments"].get(f"/payments/loans/{lid}/charges"),
        ctx.connectors["payments"].get(f"/payments/loans/{lid}/payments"),
        ctx.connectors["service_ops"].get(f"/service-ops/customers/{ctx.customer_id}/requests", {"status": "all"}))
    charge = next((c for c in charges if c["charge_id"] == args.charge_id.strip().upper()), None)
    if charge is None:
        raise ToolError(f"No charge {args.charge_id} on loan {lid}.")
    if charge["status"] == "REVERSED":
        return {"status": "not_needed", "message": f"This charge was already reversed on {f(charge['settled_date'])}."}
    existing = next((r for r in requests if r["type"] == "DISPUTE" and r["details"].get("charge_id") == charge["charge_id"]), None)
    if existing:
        return {"status": "already_raised", "request": request_brief(existing)}
    payment = next(p for p in payments if p["payment_id"] == charge["payment_id"])
    reason = pol.reference.get("payment_reason_codes", {}).get(payment["reason_code"], {})
    auto, why = reversal_eligibility(payment["reason_code"], charge, pol, ctx.today)
    days = await sla_days(ctx, "DISPUTE")
    ctx.evidence.rules(pol, "BC-1", "BC-3")
    proposal = await ctx.actions.propose(ctx, "charge_dispute", params={
        "loan_id": lid, "charge_id": charge["charge_id"], "payment_id": payment["payment_id"],
        "reason_code": payment["reason_code"], "amount": charge["total"], "charge_status": charge["status"],
        "auto_reverse": auto, "customer_reason": args.customer_reason, "sla_working_days": days},
        summary=[{"label": "Charge", "value": f"{inr(charge['total'])} bounce charge, {f(charge['charge_date'])} ({lid})"},
                 {"label": "Why it failed", "value": reason.get("label", payment["reason_code"])},
                 {"label": "Outcome", "value": "Reversed immediately under BC-3" if auto
                  else f"Reviewed by the disputes team within {days} working days"}])
    return {**proposal.for_model(), "policy_assessment": why}


@action("charge_dispute", "Bounce charge dispute", requires_otp=False)
async def execute_dispute(ctx: ToolContext, params: dict, key: str) -> dict:
    reversal = None
    if params["auto_reverse"]:
        reversal = await ctx.connectors["payments"].post(f"/payments/charges/{params['charge_id']}/reversal",
                                                         {"reason": params["reason_code"], "reference": key},
                                                         idempotency_key=f"{key}:reversal")
    created = await ctx.connectors["service_ops"].post("/service-ops/requests", {
        "customer_id": ctx.customer_id, "product_ref": params["loan_id"], "type": "DISPUTE",
        "status": "RESOLVED" if reversal else "OPEN",
        "resolution": "Bounce charge reversed under BC-3" if reversal else None,
        "summary": f"Dispute of bounce charge {params['charge_id']}", "details": params,
        "sla_working_days": params["sla_working_days"], "channel": f"assistant-{ctx.channel}"},
        idempotency_key=f"{key}:sr")
    await log_interaction(ctx, "DISPUTE", f"Dispute {created['request_id']} for charge {params['charge_id']}", key)
    result = {"request_id": created["request_id"], "status": created["status"], "due_by": fmt(iso(created["sla_due"]))}
    if reversal:
        result["reversal"] = {"amount": inr(params["amount"]),
                              "how": "Waived, so it is no longer owed" if params["charge_status"] == "OUTSTANDING"
                              else "Refunded as a credit to the loan account",
                              "emi_unchanged": "The EMI amount and schedule do not change",
                              "payment_history": "The charge now shows as reversed"}
    return result
