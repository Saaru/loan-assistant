"""Simulated enterprise systems: CRM, Loan Management, Payments, Policy Knowledge,
Service Operations and Card Management.

Each system lives under its own path prefix and gets its own latency profile and
failure rate from config/systems.yaml. A chaos endpoint lets the demo slow down or
take down any system at runtime.
"""

import asyncio
import json
import math
import os
import random
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import yaml
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from finlib.dates import add_months
from finlib.money import money

from . import seed

ROOT = Path(__file__).resolve().parent.parent
SYSTEMS_CONFIG = yaml.safe_load((ROOT / "config" / "systems.yaml").read_text())
POLICY_DIR = Path(__file__).resolve().parent / "policies"

app = FastAPI(title="Simulated core systems")
rng = random.Random()
SIMULATE = os.getenv("LA_SIMULATE", "1") == "1"  # tests switch latency/failure simulation off

# Chaos modes live in a small file so every worker process sees the same settings.
CHAOS_PATH = seed.DATA_DIR / "chaos.json"
_chaos_cache: dict = {"mtime": None, "modes": {}}


def chaos_modes() -> dict[str, str]:
    try:
        mtime = CHAOS_PATH.stat().st_mtime
    except FileNotFoundError:
        return {}
    if mtime != _chaos_cache["mtime"]:
        _chaos_cache.update(mtime=mtime, modes=json.loads(CHAOS_PATH.read_text() or "{}"))
    return _chaos_cache["modes"]


def set_chaos(modes: dict[str, str]):
    CHAOS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHAOS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(modes))
    tmp.replace(CHAOS_PATH)


# ---------------------------------------------------------------------------
# Latency and failure simulation
# ---------------------------------------------------------------------------

def system_for(path: str) -> str | None:
    for name, cfg in SYSTEMS_CONFIG["systems"].items():
        if path.startswith(cfg["prefix"] + "/") or path == cfg["prefix"]:
            return name
    return None


def profile(system: str) -> dict:
    sim = dict(SYSTEMS_CONFIG["systems"][system]["simulate"])
    sim.update(SYSTEMS_CONFIG["chaos_modes"].get(chaos_modes().get(system, "normal"), {}))
    return sim


def sample_latency_ms(p50: float, p95: float) -> float:
    sigma = (math.log(p95) - math.log(p50)) / 1.645
    return rng.lognormvariate(math.log(p50), sigma)


@app.middleware("http")
async def simulate_system_behaviour(request: Request, call_next):
    system = system_for(request.url.path)
    if system is None or not SIMULATE:
        return await call_next(request)
    sim = profile(system)
    latency = sample_latency_ms(sim["latency_ms"]["p50"], sim["latency_ms"]["p95"]) + sim.get("extra_latency_ms", 0)
    await asyncio.sleep(latency / 1000)
    if rng.random() < sim.get("error_rate", 0):
        return JSONResponse({"error": "upstream_unavailable", "system": system}, status_code=503)
    response = await call_next(request)
    if (request.method == "POST" and request.headers.get("idempotency-key") and response.status_code < 300
            and rng.random() < sim.get("lost_response_rate", 0)):
        return JSONResponse({"error": "gateway_timeout", "system": system}, status_code=504)
    response.headers["x-simulated-latency-ms"] = f"{latency:.0f}"
    return response


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

_conn: dict = {}


def db() -> sqlite3.Connection:
    """One shared connection (handlers run on the event loop); reopened when the
    database file is replaced by a reseed."""
    inode = os.stat(seed.DB_PATH).st_ino
    if _conn.get("inode") != inode:
        if _conn.get("con"):
            _conn["con"].close()
        con = sqlite3.connect(seed.DB_PATH, check_same_thread=False)
        con.row_factory = sqlite3.Row
        _conn.update(con=con, inode=inode, as_of=None)
    return _conn["con"]


def rows(sql: str, params=()) -> list[dict]:
    with db() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def one(sql: str, params=()) -> dict | None:
    found = rows(sql, params)
    return found[0] if found else None


def as_of() -> date:
    db()
    if _conn.get("as_of") is None:
        meta = one("SELECT value FROM meta WHERE key='as_of'")
        _conn["as_of"] = date.fromisoformat(meta["value"]) if meta else date.today()
    return _conn["as_of"]


def now_iso() -> str:
    return datetime.combine(as_of(), datetime.now().time()).isoformat(timespec="seconds")


def idempotent(key: str | None, create):
    """Run `create` once per idempotency key; replays return the stored response."""
    if key:
        stored = one("SELECT response FROM idempotency WHERE key=?", (key,))
        if stored:
            return {**json.loads(stored["response"]), "replayed": True}
    result = create()
    if key:
        with db() as con:
            con.execute("INSERT OR IGNORE INTO idempotency VALUES (?,?,?)", (key, json.dumps(result), now_iso()))
    return result


def add_working_days(start: date, days: int) -> date:
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def not_found(what: str):
    raise HTTPException(status_code=404, detail=f"{what} not found")


# ---------------------------------------------------------------------------
# Customer CRM
# ---------------------------------------------------------------------------

@app.get("/crm/customers")
async def crm_list_customers(demo: bool = True):
    return rows("SELECT customer_id, full_name, city, segment, demo_story FROM customers WHERE is_demo=? "
                "ORDER BY customer_id", (1 if demo else 0,))


@app.get("/crm/customers/{customer_id}")
async def crm_customer(customer_id: str):
    customer = one("SELECT * FROM customers WHERE customer_id=?", (customer_id,))
    return customer or not_found("Customer")


@app.get("/crm/customers/{customer_id}/interactions")
async def crm_interactions(customer_id: str, limit: int = 10):
    return rows("SELECT ts, channel, type, summary FROM interactions WHERE customer_id=? ORDER BY ts DESC LIMIT ?",
                (customer_id, limit))


@app.post("/crm/customers/{customer_id}/interactions")
async def crm_log_interaction(customer_id: str, body: dict = Body(...), idempotency_key: str | None = Header(None)):
    def create():
        with db() as con:
            con.execute("INSERT INTO interactions (customer_id, ts, channel, type, summary) VALUES (?,?,?,?,?)",
                        (customer_id, now_iso(), body.get("channel", "ASSISTANT"), body["type"], body["summary"]))
        return {"logged": True}
    return idempotent(idempotency_key, create)


# ---------------------------------------------------------------------------
# Loan Management
# ---------------------------------------------------------------------------

def loan_view(loan: dict) -> dict:
    schedule = rows("SELECT * FROM loan_schedule WHERE loan_id=? ORDER BY inst_no", (loan["loan_id"],))
    paid = [r for r in schedule if r["status"] == "PAID"]
    upcoming = [r for r in schedule if r["status"] != "PAID"]
    overdue_charges = one("SELECT COALESCE(SUM(CAST(total AS REAL)),0) AS t FROM charges WHERE loan_id=? "
                          "AND status='OUTSTANDING'", (loan["loan_id"],))["t"]
    return {
        **loan,
        "outstanding_principal": paid[-1]["closing"] if paid else loan["principal"],
        "emis_paid": len(paid),
        "emis_remaining": len(upcoming),
        "last_emi_due_date": paid[-1]["due_date"] if paid else None,
        "next_due_date": upcoming[0]["due_date"] if upcoming else None,
        "next_emi_amount": upcoming[0]["emi"] if upcoming else None,
        "overdue_amount": str(money(overdue_charges)),
        "as_of": as_of().isoformat(),
    }


@app.get("/loans/customers/{customer_id}")
async def loans_for_customer(customer_id: str):
    return [loan_view(l) for l in rows("SELECT * FROM loans WHERE customer_id=? ORDER BY disbursal_date", (customer_id,))]


@app.get("/loans/accounts/{loan_id}")
async def loan_account(loan_id: str):
    loan = one("SELECT * FROM loans WHERE loan_id=?", (loan_id,))
    return loan_view(loan) if loan else not_found("Loan")


@app.get("/loans/accounts/{loan_id}/schedule")
async def loan_schedule(loan_id: str, status: str | None = None, from_date: str | None = None, to_date: str | None = None):
    sql, params = "SELECT * FROM loan_schedule WHERE loan_id=?", [loan_id]
    if status:
        sql += " AND status=?"
        params.append(status.upper())
    if from_date:
        sql += " AND due_date>=?"
        params.append(from_date)
    if to_date:
        sql += " AND due_date<=?"
        params.append(to_date)
    return rows(sql + " ORDER BY inst_no", params)


@app.get("/loans/accounts/{loan_id}/events")
async def loan_events(loan_id: str):
    return [{**e, "details": json.loads(e["details"])}
            for e in rows("SELECT event_date, type, details FROM loan_events WHERE loan_id=? ORDER BY event_date", (loan_id,))]


@app.get("/loans/benchmarks")
async def benchmarks():
    return rows("SELECT * FROM benchmark_rates ORDER BY effective_from")


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

@app.get("/payments/loans/{loan_id}/payments")
async def loan_payments(loan_id: str, since: str | None = None):
    sql, params = "SELECT * FROM payments WHERE loan_id=?", [loan_id]
    if since:
        sql += " AND due_date>=?"
        params.append(since)
    return rows(sql + " ORDER BY attempt_date, payment_id", params)


@app.get("/payments/loans/{loan_id}/charges")
async def loan_charges(loan_id: str):
    return rows("SELECT * FROM charges WHERE loan_id=? ORDER BY charge_date", (loan_id,))


@app.post("/payments/charges/{charge_id}/reversal")
async def reverse_charge(charge_id: str, body: dict = Body(...), idempotency_key: str | None = Header(None)):
    charge = one("SELECT * FROM charges WHERE charge_id=?", (charge_id,))
    if not charge:
        not_found("Charge")

    def create():
        if charge["status"] == "REVERSED":
            raise HTTPException(status_code=409, detail="Charge already reversed")
        with db() as con:
            con.execute("UPDATE charges SET status='REVERSED', settled_date=? WHERE charge_id=?",
                        (as_of().isoformat(), charge_id))
        return {"charge_id": charge_id, "status": "REVERSED", "credited_amount": charge["total"],
                "reference": body.get("reference"), "credited_on": as_of().isoformat()}
    return idempotent(idempotency_key, create)


# ---------------------------------------------------------------------------
# Policy Knowledge
# ---------------------------------------------------------------------------

def load_policy(family: str) -> dict:
    path = POLICY_DIR / f"{family}.yaml"
    if not path.exists():
        not_found("Policy document")
    return yaml.safe_load(path.read_text())


@app.get("/policy/documents")
async def policy_documents():
    return [{"family": p.stem, "title": yaml.safe_load(p.read_text())["title"]} for p in sorted(POLICY_DIR.glob("*.yaml"))]


@app.get("/policy/documents/{family}")
async def policy_document(family: str, on: str | None = None):
    """Resolve the rules in force on a date: later versions override earlier ones rule by rule."""
    doc = load_policy(family)
    on_date = date.fromisoformat(on) if on else as_of()
    rules: dict[str, dict] = {}
    reference: dict = {}
    applied = None
    for version in sorted(doc["versions"], key=lambda v: str(v["effective_from"])):
        effective = version["effective_from"]
        effective = effective if isinstance(effective, date) else date.fromisoformat(str(effective))
        if effective > on_date:
            continue
        applied = version
        for rule in version.get("rules", []):
            rules[rule["id"]] = {**rule, "version": version["version"], "effective_from": effective.isoformat()}
        reference.update(version.get("reference", {}))
    if applied is None:
        not_found("Policy version for that date")
    return {"family": family, "title": doc["title"], "version": applied["version"], "resolved_on": on_date.isoformat(),
            "rules": list(rules.values()), "reference": reference}


# ---------------------------------------------------------------------------
# Service Operations
# ---------------------------------------------------------------------------

@app.get("/service-ops/customers/{customer_id}/requests")
async def service_requests(customer_id: str, status: str = "all"):
    sql = "SELECT * FROM service_requests WHERE customer_id=?"
    if status == "open":
        sql += " AND status NOT IN ('CLOSED','RESOLVED','CANCELLED')"
    return [{**r, "details": json.loads(r["details"] or "{}")} for r in rows(sql + " ORDER BY created_at DESC", (customer_id,))]


@app.get("/service-ops/requests/{request_id}")
async def service_request(request_id: str):
    r = one("SELECT * FROM service_requests WHERE request_id=?", (request_id,))
    return {**r, "details": json.loads(r["details"] or "{}")} if r else not_found("Service request")


@app.post("/service-ops/requests")
async def create_service_request(body: dict = Body(...), idempotency_key: str | None = Header(None)):
    def create():
        with db() as con:
            n = con.execute("SELECT COUNT(*) FROM service_requests").fetchone()[0]
            request_id = f"SR{104200 + n + 1}"
            created = now_iso()
            sla_due = add_working_days(as_of(), int(body.get("sla_working_days", 3))).isoformat()
            status = body.get("status", "OPEN")
            con.execute("INSERT INTO service_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                request_id, body["customer_id"], body.get("product_ref"), body["type"], status,
                body.get("priority", "NORMAL"), created, created, sla_due, body["summary"],
                json.dumps(body.get("details", {})), body.get("resolution"), idempotency_key, body.get("channel", "assistant")))
        return {"request_id": request_id, "status": status, "sla_due": sla_due, "created_at": created}
    return idempotent(idempotency_key, create)


# ---------------------------------------------------------------------------
# Card Management
# ---------------------------------------------------------------------------

def card_view(card: dict) -> dict:
    unbilled = one("SELECT COALESCE(SUM(CAST(amount AS REAL)),0) AS t FROM card_transactions WHERE card_id=? "
                   "AND status='APPROVED' AND ts>=?", (card["card_id"], card["statement_date"]))["t"]
    emi_blocked = one("SELECT COALESCE(SUM(CAST(principal AS REAL)),0) AS t FROM card_emi_plans WHERE card_id=?",
                      (card["card_id"],))["t"]
    outstanding = Decimal(card["statement_balance"]) + money(unbilled)
    available = Decimal(card["credit_limit"]) - outstanding - money(emi_blocked)
    return {**card, "intl_enabled": bool(card["intl_enabled"]), "unbilled_amount": str(money(unbilled)),
            "total_outstanding": str(money(outstanding)), "emi_blocked": str(money(emi_blocked)),
            "available_limit": str(money(max(available, Decimal(0)))), "as_of": as_of().isoformat()}


@app.get("/cards/customers/{customer_id}")
async def cards_for_customer(customer_id: str):
    return [card_view(c) for c in rows("SELECT * FROM cards WHERE customer_id=?", (customer_id,))]


@app.get("/cards/accounts/{card_id}")
async def card_account(card_id: str):
    card = one("SELECT * FROM cards WHERE card_id=?", (card_id,))
    return card_view(card) if card else not_found("Card")


@app.get("/cards/accounts/{card_id}/transactions")
async def card_transactions(card_id: str, days: int = 30, status: str | None = None):
    since = (as_of() - timedelta(days=days)).isoformat()
    sql, params = "SELECT * FROM card_transactions WHERE card_id=? AND ts>=?", [card_id, since]
    if status:
        sql += " AND status=?"
        params.append(status.upper())
    return rows(sql + " ORDER BY ts DESC", params)


@app.get("/cards/accounts/{card_id}/transactions/{txn_id}")
async def card_transaction(card_id: str, txn_id: str):
    t = one("SELECT * FROM card_transactions WHERE card_id=? AND txn_id=?", (card_id, txn_id))
    return t or not_found("Transaction")


@app.post("/cards/accounts/{card_id}/controls")
async def card_controls(card_id: str, body: dict = Body(...), idempotency_key: str | None = Header(None)):
    if not one("SELECT card_id FROM cards WHERE card_id=?", (card_id,)):
        not_found("Card")

    def create():
        with db() as con:
            con.execute("UPDATE cards SET intl_enabled=? WHERE card_id=?", (1 if body["intl_enabled"] else 0, card_id))
        return {"card_id": card_id, "intl_enabled": bool(body["intl_enabled"]), "updated_at": now_iso()}
    return idempotent(idempotency_key, create)


@app.post("/cards/accounts/{card_id}/emi-plans")
async def card_emi_plan(card_id: str, body: dict = Body(...), idempotency_key: str | None = Header(None)):
    txn = one("SELECT * FROM card_transactions WHERE card_id=? AND txn_id=?", (card_id, body["txn_id"]))
    if not txn:
        not_found("Transaction")

    def create():
        if txn["emi_plan_id"]:
            raise HTTPException(status_code=409, detail="Transaction already converted to EMI")
        with db() as con:
            n = con.execute("SELECT COUNT(*) FROM card_emi_plans").fetchone()[0]
            plan_id = f"EMI{70100 + n + 1}"
            con.execute("INSERT INTO card_emi_plans VALUES (?,?,?,?,?,?,?,?,?,?)", (
                plan_id, card_id, body["txn_id"], body["principal"], body["tenure_months"], body["annual_rate"],
                body["emi"], body["processing_fee"], body["gst"], now_iso()))
            con.execute("UPDATE card_transactions SET emi_plan_id=? WHERE txn_id=?", (plan_id, body["txn_id"]))
        return {"plan_id": plan_id, "txn_id": body["txn_id"], "first_emi_on_statement": add_months(
            date.fromisoformat(one("SELECT statement_date FROM cards WHERE card_id=?", (card_id,))["statement_date"]), 1).isoformat()}
    return idempotent(idempotency_key, create)


@app.post("/cards/accounts/{card_id}/transactions/{txn_id}/dispute")
async def card_dispute_flag(card_id: str, txn_id: str, body: dict = Body(...), idempotency_key: str | None = Header(None)):
    if not one("SELECT txn_id FROM card_transactions WHERE card_id=? AND txn_id=?", (card_id, txn_id)):
        not_found("Transaction")

    def create():
        with db() as con:
            con.execute("UPDATE card_transactions SET dispute_ref=? WHERE txn_id=?", (body["dispute_ref"], txn_id))
        return {"txn_id": txn_id, "dispute_ref": body["dispute_ref"]}
    return idempotent(idempotency_key, create)


# ---------------------------------------------------------------------------
# Admin (not subject to simulation)
# ---------------------------------------------------------------------------

@app.get("/_admin/meta")
async def admin_meta():
    return {"as_of": as_of().isoformat(), "systems": {
        name: {"display_name": cfg["display_name"], "mode": chaos_modes().get(name, "normal"), "profile": profile(name)}
        for name, cfg in SYSTEMS_CONFIG["systems"].items()}, "modes": list(SYSTEMS_CONFIG["chaos_modes"])}


@app.post("/_admin/chaos")
async def admin_chaos(body: dict = Body(...)):
    system, mode = body.get("system"), body.get("mode", "normal")
    if mode not in SYSTEMS_CONFIG["chaos_modes"]:
        raise HTTPException(status_code=400, detail=f"Unknown mode {mode}")
    systems = list(SYSTEMS_CONFIG["systems"])
    targets = systems if system in (None, "all") else [system]
    modes = dict(chaos_modes())
    for name in targets:
        if name not in systems:
            raise HTTPException(status_code=400, detail=f"Unknown system {name}")
        modes[name] = mode
    set_chaos(modes)
    return await admin_meta()


@app.post("/_admin/reseed")
async def admin_reseed():
    seed.seed()
    set_chaos({})
    return {"reseeded": True, "as_of": as_of().isoformat()}
