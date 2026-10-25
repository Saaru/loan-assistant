"""Action framework: propose -> customer confirms (OTP for high-impact actions) ->
execute once -> audit.

The model can only *propose* an action. Execution needs a confirmation from the
customer, and for high-impact actions an OTP that only the customer sees. The
action ID doubles as the idempotency key for every backend write, so retries and
double-clicks never repeat an operation.
"""

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .connectors import BackendError, Rejected
from .tools import ToolContext

OTP_TTL_S = 300
MAX_OTP_ATTEMPTS = 3


class ActionError(Exception):
    pass


Executor = Callable[[ToolContext, dict, str], Awaitable[dict]]


@dataclass
class ActionType:
    type: str
    title: str
    requires_otp: bool
    executor: Executor


ACTION_TYPES: dict[str, ActionType] = {}


def action(type: str, title: str, requires_otp: bool):
    def register(executor: Executor) -> Executor:
        ACTION_TYPES[type] = ActionType(type, title, requires_otp, executor)
        return executor
    return register


@dataclass
class PendingAction:
    action_id: str
    session_id: str
    customer_id: str
    type: str
    title: str
    params: dict
    summary: list[dict]
    requires_otp: bool
    otp_destination: str | None
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | executing | executed | failed | cancelled | expired
    otp_hash: str | None = None
    otp_attempts: int = 0
    demo_otp: str | None = None
    result: dict | None = None
    error: str | None = None

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > OTP_TTL_S

    def public(self) -> dict:
        """What the customer's app shows."""
        return {"action_id": self.action_id, "type": self.type, "title": self.title, "summary": self.summary,
                "requires_otp": self.requires_otp, "otp_destination": self.otp_destination,
                "demo_otp": self.demo_otp, "status": self.status, "result": self.result, "error": self.error,
                "expires_in_s": max(0, int(OTP_TTL_S - (time.time() - self.created_at)))}

    def for_model(self) -> dict:
        """What the model sees. Never includes the OTP."""
        return {"status": "awaiting_customer_confirmation", "action_id": self.action_id, "title": self.title,
                "summary": {row["label"]: row["value"] for row in self.summary},
                "requires_otp": self.requires_otp,
                "note": (f"A confirmation card is shown and an OTP was sent to {self.otp_destination}. Ask the customer "
                         f"to type or read out the OTP. Nothing has been executed yet." if self.requires_otp else
                         "A confirmation card is shown. No OTP is needed for this action: ask the customer to press "
                         "Confirm or say yes, and do not mention an OTP. Nothing has been executed yet.")}


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, action: PendingAction, channel: str, **extra):
        record = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, "action_id": action.action_id,
                  "type": action.type, "customer_id": action.customer_id, "session_id": action.session_id[:8],
                  "channel": channel, "params": action.params, **extra}
        with self.path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


def _hash_otp(action_id: str, otp: str) -> str:
    return hashlib.sha256(f"{action_id}:{otp}".encode()).hexdigest()


class ActionManager:
    def __init__(self, audit: AuditLog, demo_mode: bool = True):
        self.audit = audit
        self.demo_mode = demo_mode

    def pending(self, session) -> list[PendingAction]:
        return [a for a in session.actions.values() if a.status == "pending" and not a.expired]

    async def propose(self, ctx: ToolContext, type: str, params: dict, summary: list[dict],
                      otp_destination: str | None = None) -> PendingAction:
        for existing in self.pending(ctx.session):
            if existing.type == type and existing.params == params:
                return existing  # asked again for the same thing: keep one card, one OTP
        spec = ACTION_TYPES[type]
        pending = PendingAction(
            action_id=f"ACT-{secrets.token_hex(4).upper()}", session_id=ctx.session.session_id,
            customer_id=ctx.customer_id, type=type, title=spec.title, params=params, summary=summary,
            requires_otp=spec.requires_otp, otp_destination=otp_destination if spec.requires_otp else None)
        if spec.requires_otp:
            otp = f"{secrets.randbelow(10**6):06d}"
            pending.otp_hash = _hash_otp(pending.action_id, otp)
            # In production the OTP goes out by SMS; in demo mode the UI displays it.
            pending.demo_otp = otp if self.demo_mode else None
        ctx.session.actions[pending.action_id] = pending
        self.audit.write("proposed", pending, ctx.channel)
        await ctx.emit({"type": "action_proposed", "action": pending.public()})
        return pending

    async def confirm(self, ctx: ToolContext, action_id: str, otp: str | None, channel: str) -> dict:
        pending: PendingAction | None = ctx.session.actions.get(action_id)
        if pending is None or pending.customer_id != ctx.customer_id:
            raise ActionError("There is no pending action with that ID in this conversation.")
        if pending.status == "executed":
            return pending.result  # repeat confirmation: same result, no second execution
        if pending.status not in ("pending", "failed"):
            raise ActionError(f"This action is {pending.status} and can't be confirmed.")
        if pending.expired:
            pending.status = "expired"
            self.audit.write("expired", pending, channel)
            await ctx.emit({"type": "action_updated", "action": pending.public()})
            raise ActionError("The confirmation window has expired. Ask for the action again to get a new OTP.")
        if pending.requires_otp:
            if not otp:
                raise ActionError("This action needs the OTP sent to the customer's registered mobile.")
            pending.otp_attempts += 1
            if not hmac.compare_digest(pending.otp_hash, _hash_otp(action_id, otp.strip())):
                self.audit.write("otp_failed", pending, channel, attempt=pending.otp_attempts)
                if pending.otp_attempts >= MAX_OTP_ATTEMPTS:
                    pending.status = "cancelled"
                    await ctx.emit({"type": "action_updated", "action": pending.public()})
                    raise ActionError("Too many incorrect OTP attempts. The action was cancelled for security.")
                raise ActionError(f"That OTP is incorrect. {MAX_OTP_ATTEMPTS - pending.otp_attempts} attempt(s) left.")

        pending.status = "executing"
        self.audit.write("confirmed", pending, channel)
        await ctx.emit({"type": "action_updated", "action": pending.public()})
        try:
            result = await ACTION_TYPES[pending.type].executor(ctx, pending.params, pending.action_id)
        except Rejected as exc:
            ctx.session.cache.clear()
            pending.status, pending.error = "failed", str(exc)
            self.audit.write("failed", pending, channel, error=str(exc))
            await ctx.emit({"type": "action_updated", "action": pending.public()})
            raise ActionError(f"{exc.display_name} rejected the request: {exc}")
        except BackendError as exc:
            # Every write used an idempotency key, so retrying cannot duplicate anything.
            ctx.session.cache.clear()
            pending.status, pending.error = "failed", str(exc)
            self.audit.write("failed", pending, channel, error=str(exc))
            await ctx.emit({"type": "action_updated", "action": pending.public()})
            raise ActionError(f"{exc}. Nothing was duplicated; it is safe to try again.")
        ctx.session.cache.clear()  # what the customer sees next must reflect the change
        pending.status, pending.result, pending.error = "executed", result, None
        self.audit.write("executed", pending, channel, result=result)
        await ctx.emit({"type": "action_updated", "action": pending.public()})
        return result

    async def cancel(self, ctx: ToolContext, action_id: str, channel: str) -> PendingAction:
        pending = ctx.session.actions.get(action_id)
        if pending is None or pending.customer_id != ctx.customer_id:
            raise ActionError("There is no pending action with that ID in this conversation.")
        if pending.status in ("pending", "failed"):
            pending.status = "cancelled"
            self.audit.write("cancelled", pending, channel)
            await ctx.emit({"type": "action_updated", "action": pending.public()})
        return pending
