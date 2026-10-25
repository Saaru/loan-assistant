---
name: chaos-demo
description: Demonstrate how the loan assistant behaves when an enterprise backend system becomes slow or fails, using the built-in chaos switches. Use for resilience demos or when asked how failures are handled.
---

# Chaos demo

Requires the app running (see `/run-demo`). Uses only the API, so it works headlessly.

1. Create a session for CUST1002 (`POST /api/session`, see AGENTS.md) and ask a baseline question that needs the Payments system: "Show me the payments that failed in the last six months." Note the tools, reply and `total_ms`.
2. Take Payments down: `curl -s -X POST localhost:8000/api/ops/chaos -H 'Content-Type: application/json' -d '{"system":"payments","mode":"down"}'`.
3. Ask the same question again. Expected: the tool fails after its retries, the circuit breaker may open, and the assistant says plainly that payment history is unavailable right now, answers what it can, and offers to retry. It must not invent payment details. Show `GET /api/ops` (breaker state) and the failed backend spans in the turn's trace (`GET /api/traces?limit=1`).
4. Try `"mode":"slow"` on `loans` and ask "What's my next EMI?": the answer still arrives; point out the status line arrives first and the per-tool timeout (8 s) caps the wait.
5. Restore everything: `curl -s -X POST localhost:8000/api/ops/chaos -H 'Content-Type: application/json' -d '{"system":"all","mode":"normal"}'`.
6. Summarise what happened at each step and which code handled it (`app/platform/connectors.py`, the `SystemUnavailable` and timeout branches in `app/platform/orchestrator.py`).

In the browser the same switches are in the Ops panel (top right), which also shows p50/p95 latency per system and a waterfall of the last turn.
