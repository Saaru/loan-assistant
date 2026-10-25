"""Latency benchmark.

    python -m scripts.bench                      # scripted customer journeys through the chat API (needs API key)
    python -m scripts.bench --concurrency 5      # several conversations at once
    python -m scripts.bench --platform-load 200  # no LLM: N concurrent conversations hitting tools + core systems

Journey mode talks to the running app (make run) exactly as the browser does and
reads the per-turn metrics the server reports. Results go to docs/PERFORMANCE.md
and data/bench_results.json.
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JOURNEYS = [
    {"name": "EMI change, prepayment what-if, tenure follow-up", "customer": "CUST1001", "workspace": "loan_servicing",
     "turns": ["My EMI went up this month. Why?",
               "If I make a ₹50,000 prepayment today, how much interest would I save?",
               "Okay, and would paying ₹20,000 extra reduce my tenure?"]},
    {"name": "Failed payments, explanation, dispute", "customer": "CUST1002", "workspace": "loan_servicing",
     "turns": ["Show me the payments that failed in the last six months and explain what happened.",
               "The one that was the bank's fault, can you get that charge reversed?", "!confirm"]},
    {"name": "Foreclosure eligibility and options", "customer": "CUST1002", "workspace": "loan_servicing",
     "turns": ["Am I eligible for foreclosure without a penalty?",
               "Based on my current loan and the terms, what are my options?"]},
    {"name": "Card decline and international usage (second workflow)", "customer": "CUST1003",
     "workspace": "card_servicing",
     "turns": ["Why was my card declined in Dubai?", "Please turn international usage on.", "!confirm"]},
]


def stats(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"n": 0, "p50": None, "p95": None}
    pick = lambda p: values[min(len(values) - 1, max(0, round(p / 100 * len(values) + 0.5) - 1))]
    return {"n": len(values), "p50": round(pick(50)), "p95": round(pick(95)), "mean": round(statistics.mean(values))}


async def chat(client: httpx.AsyncClient, token: str, body: dict) -> dict:
    metrics, proposed = None, []
    async with client.stream("POST", "/api/chat", json=body, headers={"Authorization": f"Bearer {token}"},
                             timeout=120) as response:
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event["type"] == "turn_end":
                metrics = event["metrics"]
            elif event["type"] == "action_proposed":
                proposed.append(event["action"])
    return {"metrics": metrics, "proposed": proposed}


async def run_journey(client: httpx.AsyncClient, journey: dict, pause_s: float = 0) -> list[dict]:
    session = (await client.post("/api/session", json={"customer_id": journey["customer"],
                                                       "usecase": journey["workspace"]})).json()
    token, results, last_actions = session["token"], [], []
    for i, text in enumerate(journey["turns"]):
        if i and pause_s:
            await asyncio.sleep(pause_s)  # keeps free-tier token-per-minute limits out of the measurements
        if text == "!confirm":
            if not last_actions:
                continue
            action = last_actions[-1]
            await client.post(f"/api/actions/{action['action_id']}/confirm", json={"otp": action.get("demo_otp")},
                              headers={"Authorization": f"Bearer {token}"})
            out = await chat(client, token, {"event": {"type": "action_completed", "action_id": action["action_id"]}})
            label = "(action confirmed)"
        else:
            out = await chat(client, token, {"message": text})
            label = text
        last_actions = out["proposed"] or last_actions
        results.append({"journey": journey["name"], "turn": label, **(out["metrics"] or {})})
    return results


async def journeys(base_url: str, runs: int, concurrency: int, pause_s: float = 0):
    async with httpx.AsyncClient(base_url=base_url, timeout=120) as client:
        config = (await client.get("/api/config")).json()
        if not config["llm_configured"]:
            raise SystemExit("The app has no ANTHROPIC_API_KEY. Add it to .env and restart (make run).")
        await client.post("/api/ops/reset")
        sem = asyncio.Semaphore(concurrency)

        async def one(j):
            async with sem:
                return await run_journey(client, j, pause_s)

        started = time.perf_counter()
        batches = await asyncio.gather(*(one(j) for _ in range(runs) for j in JOURNEYS))
        wall = time.perf_counter() - started
        ops = (await client.get("/api/ops")).json()
    turns = [t for b in batches for t in b]
    return turns, wall, config, ops


def fmt_ms(v):
    return "–" if v is None else (f"{v} ms" if v < 1000 else f"{v / 1000:.2f} s")


def report(turns, wall, config, ops, runs, concurrency) -> str:
    ok = [t for t in turns if not t.get("error")]
    lines = [
        "# Performance", "",
        f"Measured {datetime.now():%d %b %Y %H:%M} on a laptop with `python -m scripts.bench --runs {runs} "
        f"--concurrency {concurrency}`. Model {config['model']}, effort `low`, simulated core systems with the latency "
        "and failure profiles in `config/systems.yaml` (failures and retries included). Free-tier model APIs limit "
        "tokens per minute, so a rate-limited round moves to the next configured model.", "",
        f"{len(turns)} turns across {len(JOURNEYS) * runs} conversations, {len(turns) - len(ok)} errors, "
        f"wall time {wall:.1f} s.", "",
        "## Per turn (customer's view)", "",
        "| Metric | p50 | p95 | n |", "|---|---|---|---|"]
    for label, key in (("First feedback (status line or text)", "first_feedback_ms"),
                       ("First word of the answer", "first_token_ms"), ("Complete answer", "total_ms"),
                       ("Time inside the LLM", "llm_ms")):
        s = stats([t.get(key) for t in ok])
        lines.append(f"| {label} | {fmt_ms(s['p50'])} | {fmt_ms(s['p95'])} | {s['n']} |")
    models: dict[str, int] = {}
    for t in ok:
        for span in t.get("spans", []):
            if span["kind"] == "llm":
                models[span["attrs"].get("model", "?")] = models.get(span["attrs"].get("model", "?"), 0) + 1
    lines += ["", "LLM rounds served by: " + ", ".join(f"`{m}` {n}" for m, n in sorted(models.items(), key=lambda x: -x[1])) + "."]
    lines += ["", "## By journey turn", "", "| Journey | Turn | Tools | First word | Complete |", "|---|---|---|---|---|"]
    by_turn: dict[tuple, list] = {}
    for t in ok:
        by_turn.setdefault((t["journey"], t["turn"]), []).append(t)
    for (journey, turn), items in by_turn.items():
        tools = round(statistics.mean(i["counts"]["tools"] for i in items), 1)
        lines.append(f"| {journey} | {turn} | {tools} | {fmt_ms(stats([i['first_token_ms'] for i in items])['p50'])} | "
                     f"{fmt_ms(stats([i['total_ms'] for i in items])['p50'])} |")
    m = ops["metrics"]
    lines += ["", "## Backend systems (from the connector layer, cache hits excluded)", "",
              "| System | p50 | p95 | calls | failed attempts |", "|---|---|---|---|---|"]
    for system, s in m["systems"].items():
        lines.append(f"| {system} | {fmt_ms(s['p50'])} | {fmt_ms(s['p95'])} | {s['n']} | {s['failed_attempts']} |")
    lines += ["", f"Backend read cache hit rate: {round(m['cache_hit_rate'] * 100)}%.", "",
              "## Tools", "", "| Tool | p50 | p95 | n |", "|---|---|---|---|"]
    for tool, s in m["tools"].items():
        lines.append(f"| {tool} | {fmt_ms(s['p50'])} | {fmt_ms(s['p95'])} | {s['n']} |")
    usage = [t["usage"] for t in ok]
    cached = sum(u["cache_read_input_tokens"] for u in usage)
    total_in = sum(u["input_tokens"] + u["cache_read_input_tokens"] + u["cache_creation_input_tokens"] for u in usage)
    lines += ["", f"Prompt cache: {round(100 * cached / max(1, total_in))}% of input tokens were served from cache.", ""]
    return "\n".join(lines)


async def platform_load(conversations: int, ramp_s: float = 10.0, think_s: tuple = (1.0, 3.0)):
    """Everything except the LLM: concurrent conversations each running a realistic tool sequence.

    Conversations start over a ramp-up period and pause between tool calls for the
    time an LLM round would take, so all of them are open at once without firing
    every request in the same millisecond."""
    from finlib.dates import business_date
    from app.platform.actions import ActionManager, AuditLog
    from app.platform.connectors import Connectors, session_cache
    from app.platform.orchestrator import Orchestrator
    from app.platform.policy import PolicyEngine
    from app.platform.session import Session
    from app.platform.telemetry import MetricsStore, TurnTrace, current_trace
    from app.platform.tools import REGISTRY
    from app.platform.usecase import load_usecases
    from app.settings import settings

    connectors = Connectors(settings.mock_base_url, settings.systems_config)
    usecases = load_usecases(settings.config_dir / "usecases")
    metrics = MetricsStore(settings.data_dir / "platform_load.jsonl")
    orch = Orchestrator(settings, connectors, PolicyEngine(connectors),
                        ActionManager(AuditLog(settings.data_dir / "platform_load_audit.jsonl")), metrics, usecases,
                        business_date)
    rng = random.Random(7)
    customers = [f"CUST{2001 + i}" for i in range(200)]
    active = {"now": 0, "peak": 0}

    async def conversation(i: int):
        await asyncio.sleep(rng.uniform(0, ramp_s))
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        try:
            await run_conversation(i)
        finally:
            active["now"] -= 1

    async def run_conversation(i: int):
        cid = rng.choice(customers)
        session = Session(f"load-{i}", cid, cid, ["loan_servicing"])
        loans = None
        for name, args in (("get_loans", {}), ("explain_emi_change", None), ("get_payment_history", {"failed_only": True}),
                           ("simulate_prepayment", None), ("check_foreclosure", None)):
            if args is None and not loans:
                return  # the conversation could not load its loans; already counted as an error
            if name != "get_loans":
                await asyncio.sleep(rng.uniform(*think_s))
            trace = TurnTrace(turn_id=f"L{i}-{name}", session_id=session.session_id, usecases=session.usecases)
            t1, t2 = current_trace.set(trace), session_cache.set(session.cache)
            try:
                if args is None:
                    args = {"loan_id": loans[0]["loan_id"]} | ({"amount": 25000} if name == "simulate_prepayment" else {})
                spec = REGISTRY[name]
                start = trace.now_ms()
                try:
                    out = await spec.handler(orch.context(session), spec.input_model.model_validate(args))
                    trace.add_span("tool", name, start)
                    if name == "get_loans":
                        loans = out["loans"]
                except Exception as exc:
                    trace.add_span("tool", name, start, ok=False)
                    trace.error = type(exc).__name__
            finally:
                trace.finish()
                metrics.add(trace)
                current_trace.reset(t1)
                session_cache.reset(t2)

    started = time.perf_counter()
    await asyncio.gather(*(conversation(i) for i in range(conversations)))
    wall = time.perf_counter() - started
    await connectors.close()
    summary = metrics.summary()
    lines = ["", f"## Platform load without the LLM ({conversations} conversations)", "",
             f"`python -m scripts.bench --platform-load {conversations}`: each conversation runs get_loans, "
             f"explain_emi_change, get_payment_history, simulate_prepayment and check_foreclosure against the "
             f"simulated core systems, with {think_s[0]:.0f} to {think_s[1]:.0f} s between calls standing in for "
             f"LLM rounds. Conversations started over {ramp_s:.0f} s; peak {active['peak']} open at once. "
             f"{len(metrics.turns)} tool calls in {wall:.1f} s ({len(metrics.turns) / wall:.0f} tool calls/s), "
             f"{summary['errors']} failed after retries.", "",
             "| Tool | p50 | p95 | n |", "|---|---|---|---|"]
    for tool, s in summary["tools"].items():
        lines.append(f"| {tool} | {fmt_ms(round(s['p50']))} | {fmt_ms(round(s['p95']))} | {s['n']} |")
    lines += ["", "| System | p50 | p95 | calls | failed attempts |", "|---|---|---|---|---|"]
    for system, s in summary["systems"].items():
        lines.append(f"| {system} | {fmt_ms(round(s['p50']))} | {fmt_ms(round(s['p95']))} | {s['n']} | {s['failed_attempts']} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--platform-load", type=int, default=0, help="Run the no-LLM load test with N conversations")
    parser.add_argument("--ramp", type=float, default=10.0, help="Seconds over which load-test conversations start")
    parser.add_argument("--pause", type=float, default=0, help="Seconds between turns (free-tier rate limits)")
    args = parser.parse_args()
    out = ROOT / "docs" / "PERFORMANCE.md"
    if args.platform_load:
        section = asyncio.run(platform_load(args.platform_load, args.ramp))
        text = out.read_text() if out.exists() else "# Performance\n"
        marker = "\n## Platform load without the LLM"
        text = text.split(marker)[0].rstrip() + "\n" + section
        out.write_text(text)
        print(section)
        return
    turns, wall, config, ops = asyncio.run(journeys(args.base_url, args.runs, args.concurrency, args.pause))
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "bench_results.json").write_text(json.dumps(turns, indent=1, default=str))
    text = report(turns, wall, config, ops, args.runs, args.concurrency)
    if out.exists() and "## Platform load without the LLM" in out.read_text():
        text += "\n## Platform load without the LLM" + out.read_text().split("## Platform load without the LLM", 1)[1]
    out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
