# AGENTS.md

Guide for AI coding agents (Claude Code, Codex, Cursor, Copilot and others) working in this repository. Humans can read it too; the README is the shorter tour.

## What this is

An AI customer-servicing assistant for a fictional lender, Northwind Finance. Customers chat or talk to it about loans and credit cards. Account facts come from tools that call six simulated enterprise systems; figures come from deterministic calculations in `finlib/`; rules come from versioned policy documents. The language model only plans tool calls and explains results. All data is synthetic.

## Set up and run

Requires Python 3.11+ (developed on 3.14) and one model API key. Groq is free with no card: https://console.groq.com/keys

```bash
make setup                 # creates .venv, installs requirements, creates .env, downloads the local voice (~60 MB)
# then put GROQ_API_KEY=gsk_... in .env (or ANTHROPIC_API_KEY / GEMINI_API_KEY). Ask the user for it; never print or commit it.
make run                   # core systems on :8100, assistant on :8000; reseeds demo data for today's date
make test                  # 24 tests, about a second, no network or API key needed
```

Without `make`: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m scripts.run`.

`make run` blocks. Start it in the background, then wait for `curl -s localhost:8000/api/health` to return `{"ok":true,...}`. `curl -s localhost:8000/api/config` shows whether a model is configured (`llm_configured`) and which voice engine is active. Ports: `LA_APP_PORT`, `LA_MOCK_PORT`.

## Verify it end to end without a browser

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/session -H 'Content-Type: application/json' \
  -d '{"customer_id":"CUST1001","usecase":"loan_servicing"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -sN -X POST localhost:8000/api/chat -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"My EMI went up this month. Why?"}'
```

The chat endpoint streams Server-Sent Events: `tool_pending`, `tool_end`, `text_delta`, `action_proposed`, `evidence` (rules, data sources, calculations), `turn_end` (latency and token counts). Concatenate the `text_delta` texts for the reply.

Demo customers and what each shows:

| Customer | Story | Try |
|---|---|---|
| CUST1001 Priya | Floating loan; EMI rose after a benchmark reset | "My EMI went up this month. Why?", "If I prepay ₹50,000 today how much interest do I save?" |
| CUST1002 Rahul | Fixed loan; three failed auto-debits, one the bank's fault | "Show the payments that failed in the last six months", then "reverse the bank's one", then "yes" |
| CUST1003 Ananya | Two loans (one in lock-in) and a card declined abroad | Start on `card_servicing`: "Why was my card declined in Dubai?", then "What's my next EMI?" (it switches workspace itself) |

Actions return an `action_proposed` event. Confirm with `POST /api/actions/{action_id}/confirm` and body `{"otp": "..."}`; in demo mode the OTP is in the event's `action.demo_otp` and never in the model's context.

Other useful endpoints: `GET /api/ops` (systems, breakers, latency summary), `POST /api/ops/chaos` with `{"system":"payments","mode":"down"}` (modes: normal, slow, flaky, down), `POST /api/ops/reset` (reseed demo data), `GET /api/traces`.

The free Groq tier allows about 8,000 tokens per minute per model and each turn uses 3,000 to 5,000 per call, so pause a few seconds between scripted turns. The app falls back across `gpt-oss-120b`, `gpt-oss-20b` and Qwen on rate limits.

## Repository map

```
app/main.py                      HTTP API: sessions, chat and voice streams, actions, ops
app/platform/orchestrator.py     the agent loop: model round, parallel tools, events, tracing, workspace switching
app/platform/tools.py            @tool registry, ToolContext, EvidenceCollector
app/platform/actions.py          @action registry: propose, confirm (OTP), execute once, audit
app/platform/connectors.py       per-system HTTP client: timeout, retries, circuit breaker, read cache
app/platform/policy.py           versioned policy documents and rule search
app/platform/usecase.py          loads config/usecases/*.yaml packs; context and panel providers
app/platform/llm/                model providers (Anthropic, OpenAI-compatible) behind one interface
app/platform/voice.py            speech-to-text, local Piper speech, speakable() text normalisation
app/platform/prompts.py          system prompt, voice style
app/domains/lending/             loan tools (tools.py) and pure business rules (rules.py)
app/domains/cards/               card tools: the second workflow
app/domains/common/              tools every workspace shares
app/web/                         plain HTML/CSS/JS app; voice.js is the voice client
config/usecases/*.yaml           one pack per workflow: tools, policies, instructions, panels, prompts
config/systems.yaml              simulated latency/failures and client timeouts/retries per system
finlib/                          Decimal money, dates, amortization (shared by mock systems and assistant)
mock_systems/app.py              the six simulated systems (FastAPI routers by path prefix)
mock_systems/seed.py             synthetic data generator (3 story customers + 200 background)
mock_systems/policies/*.yaml     versioned product terms served by the Policy Knowledge system
scripts/run.py, scripts/bench.py launcher and latency benchmark
tests/                           unit, integration and conversation-loop tests (no network)
docs/ARCHITECTURE.md             design, decisions, trade-offs; docs/PERFORMANCE.md measurements
```

## Rules for changing the code

- **The model never does maths.** Money and dates go through `finlib/` (Decimal, explicit rounding). Tools return display-ready strings via `finlib.money.inr()`.
- **Customer identity comes from the session.** Tools use `ctx.customer_id`; any loan or card ID from the model is checked against that customer's products (`owned_loan`, `owned_card`). Never add a tool input for customer ID.
- **Tools** are async functions decorated with `@tool(name, description, label, input_model=..., kind="read"|"action")`. Keep descriptions to one line: every token is sent on every model call. Record evidence with `ctx.evidence.data/rules/calculation`.
- **Actions** never execute inside a tool. A tool calls `ctx.actions.propose(...)` and returns `pending.for_model()`; the `@action` executor runs only after confirmation. Every backend write passes `idempotency_key=f"{key}:<step>"`.
- **Business rules read their parameters from policy YAML** (`pol.rule("PP-2").params`), so a fee or limit change is a policy edit with a new version and `effective_from`, not a code change.
- **Conversation history is append-only** (the orchestrator rolls back a failed turn rather than editing earlier messages).
- Run `make test` before finishing. Add a test next to the closest existing one in `tests/`.
- Never commit `.env`, anything in `data/`, or real personal data. Keep all names and data fictional.

## Common tasks

- **New workflow** (collections, insurance...): copy the card pack. Add `app/domains/<name>/tools.py`, `config/usecases/<id>.yaml`, policy YAML in `mock_systems/policies/`, data and routes in `mock_systems/` if it needs a new system (plus its entry in `config/systems.yaml`), and panel renderers in `app/web/app.js` `PANELS`. The tab, the `switch_workspace` routing and the UI come for free. Claude Code skill: `/add-workflow`.
- **New action:** `@action` executor plus a proposing `@tool(kind="action")`, listed in the pack YAML. Skill: `/add-action`.
- **Fee or rule change:** add a version block to the policy YAML with `effective_from`; tools pick up the version in force on the relevant date.
- **Slower or flakier backends:** `config/systems.yaml` or the Ops panel's chaos modes.
- **Another model provider:** set `LA_LLM_PROVIDER` / `LA_MODEL` in `.env` (see `.env.example`); providers live in `app/platform/llm/`.

## Troubleshooting

- `llm_configured: false`: no key in `.env`; restart after adding it.
- "All configured models are at their free-tier rate limit": wait a minute; scripted runs should pause between turns.
- Voice: needs Chrome and microphone permission; on macOS also System Settings > Privacy & Security > Microphone for the browser. `make voices` if the voice engine shows `none`.
- Port in use: `lsof -nP -iTCP:8000 -sTCP:LISTEN`, or set `LA_APP_PORT` / `LA_MOCK_PORT`.
- Stale or changed demo data: `POST /api/ops/reset` or `make seed`.
