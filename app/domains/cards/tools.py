"""Credit card servicing tools.

Built on the same platform as loan servicing: the same connectors (with retries,
breaker, cache), policy engine, amortization library, action framework and
telemetry. Only the card-specific reads and rules live here.
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from finlib import amortization as am
from finlib.dates import add_months, fmt
from finlib.money import inr, money

from ...platform.actions import action
from ...platform.tools import NoArgs, ToolContext, ToolError, tool
from ...platform.usecase import context_provider, panel
from ..common.tools import log_interaction, profile, request_brief

FAMILY = "credit_card"


def D(value) -> Decimal:
    return Decimal(str(value))


def day(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


async def customer_cards(ctx: ToolContext) -> list[dict]:
    cards = await ctx.connectors["cards"].get(f"/cards/customers/{ctx.customer_id}")
    ctx.evidence.data("Card Management", "Card accounts")
    return cards


async def owned_card(ctx: ToolContext, card_id: str) -> dict:
    card_id = card_id.strip().upper()
    for card in await customer_cards(ctx):
        if card["card_id"] == card_id:
            return card
    raise ToolError(f"No card {card_id} on this customer's profile. Use list_cards to see their cards.")


async def owned_txn(ctx: ToolContext, card: dict, txn_id: str) -> dict:
    txns = await ctx.connectors["cards"].get(f"/cards/accounts/{card['card_id']}/transactions", {"days": 120})
    txn = next((t for t in txns if t["txn_id"] == txn_id.strip().upper()), None)
    if txn is None:
        raise ToolError(f"No transaction {txn_id} on card ending {card['last4']} in the last 120 days.")
    return txn


async def policy(ctx: ToolContext):
    doc = await ctx.policy.document(FAMILY, ctx.today)
    ctx.evidence.data("Policy Knowledge", f"{doc.title} (v{doc.version})")
    return doc


def card_brief(c: dict) -> dict:
    return {"card_id": c["card_id"], "product": c["product_name"], "ending": c["last4"],
            "credit_limit": inr(c["credit_limit"]), "available_limit": inr(c["available_limit"]),
            "total_outstanding": inr(c["total_outstanding"]), "unbilled": inr(c["unbilled_amount"]),
            "last_statement": {"date": fmt(day(c["statement_date"]).date()), "balance": inr(c["statement_balance"]),
                               "minimum_due": inr(c["min_due"]), "due_on": fmt(day(c["due_date"]).date())},
            "blocked_for_emis": inr(c["emi_blocked"]),
            "international_usage": "On" if c["intl_enabled"] else "Off"}


def emi_eligibility(txn: dict, pol, today) -> tuple[bool, str]:
    p = pol.rule("CC-EMI-1").params
    if txn["status"] != "APPROVED":
        return False, "Only approved purchases can be converted."
    if txn["emi_plan_id"]:
        return False, f"Already converted ({txn['emi_plan_id']})."
    if txn["dispute_ref"]:
        return False, "Under dispute."
    if txn["category"] in p["excluded_categories"]:
        return False, f"{txn['category'].title()} purchases can't be converted [CC-EMI-1]."
    if D(txn["amount"]) < D(p["min_amount"]):
        return False, f"Below the {inr(p['min_amount'])} minimum [CC-EMI-1]."
    age = (today - day(txn["ts"]).date()).days
    if age > p["max_days_since_purchase"]:
        return False, f"More than {p['max_days_since_purchase']} days old [CC-EMI-1]."
    return True, f"Eligible until {fmt(day(txn['ts']).date() + timedelta(days=p['max_days_since_purchase']))} [CC-EMI-1]."


def txn_brief(t: dict, codes: dict, pol, today) -> dict:
    out = {"txn_id": t["txn_id"], "date": fmt(day(t["ts"]).date()), "merchant": t["merchant"],
           "category": t["category"].title(), "amount": inr(t["amount"]), "status": t["status"]}
    if t["orig_currency"] and t["orig_currency"] != "INR":
        out["original_amount"] = f"{t['orig_currency']} {t['orig_amount']}"
        out["country"] = t["country"]
    if t["status"] == "DECLINED":
        ref = codes.get(t["decline_code"], {})
        out["decline"] = {"reason": ref.get("label", t["decline_code"]), "what_happened": ref.get("explanation"),
                          "fix": ref.get("advice")}
    else:
        eligible, note = emi_eligibility(t, pol, today)
        if eligible and D(t["amount"]) >= D(pol.rule("CC-EMI-1").params["min_amount"]):
            out["emi_conversion"] = note
    if t["emi_plan_id"]:
        out["emi_plan"] = t["emi_plan_id"]
    if t["dispute_ref"]:
        out["dispute"] = t["dispute_ref"]
    return out


@context_provider("cards")
async def cards_context(ctx: ToolContext) -> str:
    cards = await customer_cards(ctx)
    if not cards:
        return "Credit cards: none."
    return "Credit cards: " + "; ".join(f"{c['card_id']} {c['product_name']} ending {c['last4']}" for c in cards) + "."


@panel("cards")
async def cards_panel(ctx: ToolContext) -> dict:
    return {"items": [{"card_id": c["card_id"], "product": c["product_name"], "last4": c["last4"],
                       "limit": inr(c["credit_limit"]), "available": inr(c["available_limit"]),
                       "statement_balance": inr(c["statement_balance"]), "min_due": inr(c["min_due"]),
                       "due_date": fmt(day(c["due_date"]).date()), "intl_enabled": c["intl_enabled"]}
                      for c in await customer_cards(ctx)]}


@panel("card_transactions")
async def card_txn_panel(ctx: ToolContext) -> dict:
    cards = await customer_cards(ctx)
    batches = await asyncio.gather(*(ctx.connectors["cards"].get(f"/cards/accounts/{c['card_id']}/transactions",
                                                                  {"days": 30}) for c in cards))
    items = []
    for card, txns in zip(cards, batches):
        for t in txns:
            note = t["decline_code"].replace("_", " ").lower() if t["decline_code"] else (
                f"EMI {t['emi_plan_id']}" if t["emi_plan_id"] else ("disputed" if t["dispute_ref"] else None))
            items.append({"ts": t["ts"], "date": fmt(day(t["ts"]).date()), "merchant": t["merchant"],
                          "amount": inr(t["amount"]), "status": t["status"], "last4": card["last4"], "note": note})
    items.sort(key=lambda t: t["ts"], reverse=True)
    return {"items": items[:8]}


@tool("list_cards", "The customer's cards: limit, available, outstanding, statement, minimum due, due date, "
      "international usage.", label="Fetching your cards")
async def list_cards(ctx: ToolContext, args: NoArgs) -> dict:
    return {"cards": [card_brief(c) for c in await customer_cards(ctx)], "as_of": fmt(ctx.today)}


class TxnArgs(BaseModel):
    card_id: str
    days: int = Field(30, ge=1, le=120)
    status: Literal["all", "declined", "approved"] = "all"
    merchant: str | None = Field(None, description="Partial merchant name")


@tool("get_card_transactions", "Card transactions with plain reasons for declines and EMI eligibility.",
      label="Checking your card transactions", input_model=TxnArgs)
async def get_card_transactions(ctx: ToolContext, args: TxnArgs) -> dict:
    card = await owned_card(ctx, args.card_id)
    params = {"days": args.days, "status": None if args.status == "all" else args.status.upper()}
    txns, pol = await asyncio.gather(
        ctx.connectors["cards"].get(f"/cards/accounts/{card['card_id']}/transactions", params), policy(ctx))
    if args.merchant:
        txns = [t for t in txns if args.merchant.lower() in t["merchant"].lower()]
    codes = pol.reference.get("card_decline_codes", {})
    ctx.evidence.data("Card Management", f"Transactions on card ending {card['last4']}")
    ctx.evidence.rules(pol, "CC-EMI-1", *(["CC-INTL-1"] if any(t["decline_code"] == "INTL_USAGE_DISABLED" for t in txns) else []))
    return {"card": f"{card['product_name']} ending {card['last4']}", "period_days": args.days,
            "international_usage": "On" if card["intl_enabled"] else "Off",
            "transactions": [txn_brief(t, codes, pol, ctx.today) for t in txns[:25]],
            **({"note": f"Showing 25 of {len(txns)}"} if len(txns) > 25 else {})}


class CardEmiArgs(BaseModel):
    card_id: str
    txn_id: str
    tenure_months: Literal[3, 6, 9, 12] | None = Field(None, description="Omit to compare all tenures")


async def emi_quote(ctx: ToolContext, card_id: str, txn_id: str):
    card = await owned_card(ctx, card_id)
    txn, pol = await asyncio.gather(owned_txn(ctx, card, txn_id), policy(ctx))
    eligible, note = emi_eligibility(txn, pol, ctx.today)
    return card, txn, pol, eligible, note


def emi_plan(amount: Decimal, months: int, p: dict, first_due) -> dict:
    rate = D(p["annual_rate_pct"])
    emi = am.emi(amount, rate, months)
    rows = am.schedule(amount, rate, emi, first_due, max_months=months)
    interest = am.total_interest(rows)
    fee = max(money(amount * D(p["processing_fee_pct"]) / 100), D(p["processing_fee_min"]))
    gst = money(fee * D(p["gst_pct"]) / 100)
    return {"months": months, "emi": emi, "interest": interest, "fee": fee, "gst": gst,
            "total_cost": interest + fee + gst}


@tool("simulate_card_emi", "Convert-to-EMI quote for a card purchase: eligibility under the card terms, EMI, total "
      "interest, processing fee and total cost for each tenure.", label="Calculating EMI options",
      input_model=CardEmiArgs)
async def simulate_card_emi(ctx: ToolContext, args: CardEmiArgs) -> dict:
    card, txn, pol, eligible, note = await emi_quote(ctx, args.card_id, args.txn_id)
    p = pol.rule("CC-EMI-2").params
    amount = D(txn["amount"])
    first_due = add_months(day(card["statement_date"]).date(), 1)
    tenures = [args.tenure_months] if args.tenure_months else p["tenures"]
    plans = [emi_plan(amount, t, p, first_due) for t in tenures]
    ctx.evidence.rules(pol, "CC-EMI-1", "CC-EMI-2", "CC-EMI-3")
    ctx.evidence.calculation(
        f"EMI options for {txn['merchant']} ({inr(amount)})",
        {"Amount": inr(amount), "Rate": f"{p['annual_rate_pct']}% a year, reducing balance",
         "Processing fee": f"{p['processing_fee_pct']}% (min {inr(p['processing_fee_min'])}) + {p['gst_pct']}% GST"},
        {f"{x['months']} months": f"{inr(x['emi'])}/month, total cost {inr(x['total_cost'])}" for x in plans},
        "Same reducing-balance amortization engine as loans; fee and GST billed once in the next statement")
    return {"transaction": {"txn_id": txn["txn_id"], "merchant": txn["merchant"], "amount": inr(amount),
                            "date": fmt(day(txn["ts"]).date())},
            "eligible": eligible, "eligibility_note": note,
            "options": [{"tenure_months": x["months"], "monthly_emi": inr(x["emi"]), "total_interest": inr(x["interest"]),
                         "processing_fee_with_gst": inr(x["fee"] + x["gst"]), "total_cost_of_emi": inr(x["total_cost"])}
                        for x in plans],
            "first_emi_on_statement_of": fmt(first_due), "limit_note": "The converted amount stays blocked from the "
            "limit and is released as each EMI's principal is paid [CC-EMI-3]."}


class ConvertArgs(BaseModel):
    card_id: str
    txn_id: str
    tenure_months: Literal[3, 6, 9, 12]


@tool("convert_to_emi", "Prepare an EMI conversion once the customer picks a tenure (OTP).",
      label="Preparing EMI conversion", input_model=ConvertArgs, kind="action")
async def convert_to_emi(ctx: ToolContext, args: ConvertArgs) -> dict:
    (card, txn, pol, eligible, note), who = await asyncio.gather(emi_quote(ctx, args.card_id, args.txn_id), profile(ctx))
    if not eligible:
        return {"status": "not_allowed", "reason": note}
    p = pol.rule("CC-EMI-2").params
    plan = emi_plan(D(txn["amount"]), args.tenure_months, p, add_months(day(card["statement_date"]).date(), 1))
    proposal = await ctx.actions.propose(ctx, "card_emi_conversion", params={
        "card_id": card["card_id"], "txn_id": txn["txn_id"], "tenure_months": args.tenure_months,
        "principal": str(D(txn["amount"])), "annual_rate": str(p["annual_rate_pct"]), "emi": str(plan["emi"]),
        "processing_fee": str(plan["fee"]), "gst": str(plan["gst"]), "merchant": txn["merchant"]},
        summary=[{"label": "Purchase", "value": f"{txn['merchant']}, {inr(txn['amount'])}"},
                 {"label": "Plan", "value": f"{args.tenure_months} EMIs of {inr(plan['emi'])} at {p['annual_rate_pct']}%"},
                 {"label": "Processing fee", "value": f"{inr(plan['fee'] + plan['gst'])} incl. GST"},
                 {"label": "Total interest", "value": inr(plan["interest"])},
                 {"label": "Card", "value": f"ending {card['last4']}"}],
        otp_destination=who["mobile"])
    return proposal.for_model()


@action("card_emi_conversion", "Convert purchase to EMI", requires_otp=True)
async def execute_emi_conversion(ctx: ToolContext, params: dict, key: str) -> dict:
    created = await ctx.connectors["cards"].post(f"/cards/accounts/{params['card_id']}/emi-plans", {
        "txn_id": params["txn_id"], "principal": params["principal"], "tenure_months": params["tenure_months"],
        "annual_rate": params["annual_rate"], "emi": params["emi"], "processing_fee": params["processing_fee"],
        "gst": params["gst"]}, idempotency_key=f"{key}:plan")
    await log_interaction(ctx, "CARD_EMI", f"Converted {params['merchant']} purchase to EMI plan {created['plan_id']}", key)
    return {"plan_id": created["plan_id"], "status": "ACTIVE",
            "first_emi_on_statement_of": fmt(day(created["first_emi_on_statement"]).date())}


class IntlArgs(BaseModel):
    card_id: str
    enabled: bool


@tool("set_international_usage", "Prepare turning international usage on or off (OTP).",
      label="Preparing card settings change", input_model=IntlArgs, kind="action")
async def set_international_usage(ctx: ToolContext, args: IntlArgs) -> dict:
    card, pol, who = await asyncio.gather(owned_card(ctx, args.card_id), policy(ctx), profile(ctx))
    if card["intl_enabled"] == args.enabled:
        return {"status": "no_change", "message": f"International usage is already {'on' if args.enabled else 'off'}."}
    ctx.evidence.rules(pol, "CC-INTL-1")
    markup = pol.rule("CC-INTL-1").params["markup_pct"]
    proposal = await ctx.actions.propose(ctx, "card_intl_usage", params={"card_id": card["card_id"], "enabled": args.enabled},
                                         summary=[{"label": "Card", "value": f"{card['product_name']} ending {card['last4']}"},
                                                  {"label": "International usage", "value": "Turn ON" if args.enabled else "Turn OFF"},
                                                  *([{"label": "Note", "value": f"{markup}% markup + GST on foreign currency spends"}]
                                                    if args.enabled else [])],
                                         otp_destination=who["mobile"])
    return proposal.for_model()


@action("card_intl_usage", "Change international usage", requires_otp=True)
async def execute_intl(ctx: ToolContext, params: dict, key: str) -> dict:
    updated = await ctx.connectors["cards"].post(f"/cards/accounts/{params['card_id']}/controls",
                                                 {"intl_enabled": params["enabled"]}, idempotency_key=f"{key}:controls")
    await log_interaction(ctx, "CARD_CONTROL", f"International usage {'enabled' if params['enabled'] else 'disabled'}", key)
    return {"status": "UPDATED", "international_usage": "On" if updated["intl_enabled"] else "Off"}


class CardDisputeArgs(BaseModel):
    card_id: str
    txn_id: str
    reason: str


@tool("raise_card_dispute", "Prepare a dispute for a transaction the customer doesn't recognise (customer confirms).",
      label="Preparing your dispute", input_model=CardDisputeArgs, kind="action")
async def raise_card_dispute(ctx: ToolContext, args: CardDisputeArgs) -> dict:
    card = await owned_card(ctx, args.card_id)
    txn, pol, requests = await asyncio.gather(owned_txn(ctx, card, args.txn_id), policy(ctx),
                                              ctx.connectors["service_ops"].get(
                                                  f"/service-ops/customers/{ctx.customer_id}/requests", {"status": "all"}))
    existing = next((r for r in requests if r["type"] == "CARD_DISPUTE" and r["details"].get("txn_id") == txn["txn_id"]), None)
    if existing or txn["dispute_ref"]:
        return {"status": "already_raised", **({"request": request_brief(existing)} if existing else {})}
    p = pol.rule("CC-DSP-1").params
    age = (ctx.today - day(txn["ts"]).date()).days
    ctx.evidence.rules(pol, "CC-DSP-1")
    if age > p["window_days"]:
        return {"status": "not_allowed", "reason": f"The transaction is {age} days old; disputes must be raised within "
                                                   f"{p['window_days']} days [CC-DSP-1]."}
    proposal = await ctx.actions.propose(ctx, "card_dispute", params={
        "card_id": card["card_id"], "txn_id": txn["txn_id"], "merchant": txn["merchant"], "amount": txn["amount"],
        "reason": args.reason, "sla_working_days": p["sla_working_days"]},
        summary=[{"label": "Transaction", "value": f"{txn['merchant']}, {inr(txn['amount'])} on {fmt(day(txn['ts']).date())}"},
                 {"label": "Reason", "value": args.reason},
                 {"label": "What happens", "value": f"Temporary credit within {p['temporary_credit_working_days']} working "
                                                    f"days; decision within {p['sla_working_days']} working days"}])
    return proposal.for_model()


@action("card_dispute", "Card transaction dispute", requires_otp=False)
async def execute_card_dispute(ctx: ToolContext, params: dict, key: str) -> dict:
    created = await ctx.connectors["service_ops"].post("/service-ops/requests", {
        "customer_id": ctx.customer_id, "product_ref": params["card_id"], "type": "CARD_DISPUTE",
        "summary": f"Dispute: {params['merchant']} {inr(params['amount'])}", "details": params,
        "sla_working_days": params["sla_working_days"], "channel": f"assistant-{ctx.channel}"},
        idempotency_key=f"{key}:sr")
    await ctx.connectors["cards"].post(f"/cards/accounts/{params['card_id']}/transactions/{params['txn_id']}/dispute",
                                       {"dispute_ref": created["request_id"]}, idempotency_key=f"{key}:flag")
    await log_interaction(ctx, "CARD_DISPUTE", f"Dispute {created['request_id']} for {params['merchant']}", key)
    return {"request_id": created["request_id"], "status": created["status"], "due_by": fmt(day(created["sla_due"]).date())}
