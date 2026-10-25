"""Capabilities shared by every servicing workspace."""

import asyncio
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from finlib.dates import fmt

from ...platform.actions import ActionError, action
from ...platform.connectors import SystemUnavailable
from ...platform.tools import NoArgs, ToolContext, tool
from ...platform.usecase import context_provider, panel


def mask_phone(phone: str) -> str:
    return f"{phone[:3]} ******{phone[-4:]}" if phone else ""


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else ""


def short_date(iso: str | None) -> str | None:
    if not iso:
        return None
    return fmt(datetime.fromisoformat(iso).date())


async def profile(ctx: ToolContext) -> dict:
    c = await ctx.connectors["crm"].get(f"/crm/customers/{ctx.customer_id}")
    ctx.evidence.data("Customer CRM", "Customer profile")
    return {"name": c["full_name"], "city": c["city"], "customer_since": short_date(c["customer_since"]),
            "mobile": mask_phone(c["phone"]), "email": mask_email(c["email"]), "kyc_status": c["kyc_status"],
            "segment": c["segment"]}


async def sla_days(ctx: ToolContext, request_type: str, default: int = 3) -> int:
    """Turnaround for a request type, from whichever policy family defines it."""
    try:
        doc = await ctx.policy.document("personal_loan", ctx.today)
        return int(doc.rule("SR-1").params["sla_working_days"].get(request_type, default))
    except (SystemUnavailable, KeyError):
        return default


async def log_interaction(ctx: ToolContext, type_: str, summary: str, key: str):
    """Best effort: a CRM note must not undo a completed action."""
    try:
        await ctx.connectors["crm"].post(f"/crm/customers/{ctx.customer_id}/interactions",
                                         {"channel": "ASSISTANT", "type": type_, "summary": summary},
                                         idempotency_key=f"{key}:crm")
    except Exception:
        pass


@context_provider("customer")
async def customer_context(ctx: ToolContext) -> str:
    p = await profile(ctx)
    return f"Name: {p['name']} ({p['segment']}, {p['city']}), customer since {p['customer_since']}. Registered mobile {p['mobile']}."


@panel("profile")
async def profile_panel(ctx: ToolContext) -> dict:
    return await profile(ctx)


@panel("requests")
async def requests_panel(ctx: ToolContext) -> dict:
    items = await ctx.connectors["service_ops"].get(f"/service-ops/customers/{ctx.customer_id}/requests",
                                                    {"status": "all"})
    return {"items": [request_brief(r) for r in items[:6]]}


def request_brief(r: dict) -> dict:
    return {"request_id": r["request_id"], "type": r["type"], "status": r["status"], "summary": r["summary"],
            "product": r["product_ref"], "raised_on": short_date(r["created_at"]), "due_by": short_date(r["sla_due"]),
            "resolution": r.get("resolution")}


@tool("get_customer_profile", "Get the signed-in customer's profile: name, city, customer since, masked contact "
      "details and KYC status.", label="Looking up your profile")
async def get_customer_profile(ctx: ToolContext, args: NoArgs) -> dict:
    return await profile(ctx)


class InteractionArgs(BaseModel):
    limit: int = Field(8, ge=1, le=20, description="How many recent items to return")


@tool("get_recent_notices", "Recent messages and contacts with the customer from the CRM: rate revision notices, "
      "EMI bounce alerts, mandate alerts, past requests. Useful to say when and how the customer was informed.",
      label="Checking recent notices", input_model=InteractionArgs)
async def get_recent_notices(ctx: ToolContext, args: InteractionArgs) -> dict:
    items = await ctx.connectors["crm"].get(f"/crm/customers/{ctx.customer_id}/interactions", {"limit": args.limit})
    ctx.evidence.data("Customer CRM", "Interaction history")
    return {"notices": [{"date": short_date(i["ts"]), "channel": i["channel"], "type": i["type"],
                         "summary": i["summary"]} for i in items]}


class RequestListArgs(BaseModel):
    status: Literal["open", "all"] = "open"


@tool("list_service_requests", "The customer's service requests and their status.",
      label="Checking your service requests", input_model=RequestListArgs)
async def list_service_requests(ctx: ToolContext, args: RequestListArgs) -> dict:
    items = await ctx.connectors["service_ops"].get(f"/service-ops/customers/{ctx.customer_id}/requests",
                                                    {"status": args.status})
    ctx.evidence.data("Service Operations", "Service requests")
    return {"requests": [request_brief(r) for r in items]}


class PolicyArgs(BaseModel):
    query: str = Field(..., description="A few keywords")


@tool("lookup_policy", "Search the product terms for general policy questions; returns rule IDs and text to cite.",
      label="Checking the product terms", input_model=PolicyArgs)
async def lookup_policy(ctx: ToolContext, args: PolicyArgs) -> dict:
    families = ctx.extras.get("policy_families") or []
    docs = await asyncio.gather(*(ctx.policy.document(f, ctx.today) for f in families))
    matches = []
    for doc in docs:
        for rule in doc.search(args.query):
            ctx.evidence.rule(rule)
            matches.append({"rule_id": rule.id, "title": rule.title, "text": rule.text, "document": doc.title,
                            "version": rule.version, "effective_from": rule.effective_from,
                            **({"change_note": rule.change_note} if rule.change_note else {})})
    if not matches:
        return {"matches": [], "note": "No rule matched. Say you couldn't find it in the terms and offer a callback."}
    return {"matches": matches[:5], "stale": any(d.stale for d in docs)}


class ServiceRequestArgs(BaseModel):
    category: Literal["MANDATE_UPDATE", "TENURE_CHANGE", "STATEMENT", "CALLBACK", "GENERAL"]
    description: str = Field(..., description="One line, in the customer's terms")
    product_ref: str | None = Field(None, description="Loan or card ID")


@tool("create_service_request", "Prepare a service request (customer confirms): new mandate, tenure change, "
      "statement, callback or other.", label="Preparing a service request", input_model=ServiceRequestArgs,
      kind="action")
async def create_service_request(ctx: ToolContext, args: ServiceRequestArgs) -> dict:
    days = await sla_days(ctx, args.category)
    pending = await ctx.actions.propose(ctx, "service_request", params=args.model_dump() | {"sla_working_days": days},
                                        summary=[{"label": "Request", "value": args.category.replace("_", " ").title()},
                                                 {"label": "Details", "value": args.description},
                                                 *([{"label": "For", "value": args.product_ref}] if args.product_ref else []),
                                                 {"label": "Turnaround", "value": f"{days} working days"}])
    return pending.for_model()


@action("service_request", "Raise a service request", requires_otp=False)
async def execute_service_request(ctx: ToolContext, params: dict, key: str) -> dict:
    created = await ctx.connectors["service_ops"].post("/service-ops/requests", {
        "customer_id": ctx.customer_id, "product_ref": params.get("product_ref"), "type": params["category"],
        "summary": params["description"], "details": params, "sla_working_days": params["sla_working_days"],
        "channel": f"assistant-{ctx.channel}"}, idempotency_key=f"{key}:sr")
    await log_interaction(ctx, "SERVICE_REQUEST", f"{params['category']} request {created['request_id']} raised via assistant", key)
    return {"request_id": created["request_id"], "status": created["status"], "due_by": fmt_iso(created["sla_due"])}


class ConfirmArgs(BaseModel):
    action_id: str | None = Field(None, description="Only needed if several actions are pending")
    otp: str | None = Field(None, description="Exactly as the customer typed or said it")


@tool("confirm_action", "Confirm the pending action the customer just approved. Never invent an OTP.",
      label="Confirming your request", input_model=ConfirmArgs, kind="action")
async def confirm_action(ctx: ToolContext, args: ConfirmArgs) -> dict:
    pending = ctx.actions.pending(ctx.session)
    action_id = args.action_id if args.action_id in ctx.session.actions else None
    if action_id is None:
        if not pending:
            return {"status": "not_confirmed", "reason": "Nothing is waiting for confirmation."}
        if len(pending) > 1 and args.action_id:
            return {"status": "not_confirmed", "reason": "Several actions are pending; say which one.",
                    "pending": [{"action_id": a.action_id, "title": a.title} for a in pending]}
        action_id = pending[-1].action_id  # the one the customer was just asked about
    try:
        result = await ctx.actions.confirm(ctx, action_id, args.otp, channel=f"chat-{ctx.channel}")
    except ActionError as exc:
        return {"status": "not_confirmed", "reason": str(exc)}
    return {"status": "executed", "result": result}


def fmt_iso(iso: str | None) -> str | None:
    return fmt(datetime.fromisoformat(iso).date()) if iso else None
