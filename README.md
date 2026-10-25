# Loan Assistant

An AI customer-servicing assistant for a lender (the fictional "Northwind Finance"). Customers chat with it to understand their loans, get exact customer-specific answers, and complete service requests across several back-end systems. A second workflow, credit card servicing, runs on the same platform to show how new use cases plug in without rebuilding the core.

It is not a chatbot over documents. Answers come from the customer's own account data, deterministic financial calculations and versioned policy rules. The language model plans which tools to call and explains the results.

- **Precise answers**: "Why did my EMI go up?", "What if I prepay ₹50,000?", "Can I foreclose without a penalty?" are answered from live data with the rule IDs and calculations behind them.
- **Actions with real controls**: prepayment requests, charge disputes, card controls and EMI conversions need customer confirmation, and an OTP when high-impact. Each one runs exactly once and is audited.
- **Built for unreliable systems**: five simulated enterprise systems with their own latency and failure rates, plus a chaos switch to slow down or take down any of them live.
- **Live voice**: talk to the same assistant. It speaks a short acknowledgement while it looks things up, answers in a sentence or two, and stops the moment you talk over it, carrying on with what you said instead.
- **Measured**: every turn is traced (LLM rounds, tools, backend calls) and the numbers are shown in the UI and in [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, decisions and trade-offs.

## Quick start

Requires Python 3.11+ and an API key for one model provider. The assistant is model-agnostic: Claude, or any OpenAI-compatible API such as Groq (free tier, no card), Google Gemini (free tier) or a local Ollama model.

```bash
make setup                 # creates .venv, installs dependencies, creates .env
# put one key in .env, e.g. GROQ_API_KEY=gsk_...  (free at https://console.groq.com/keys)
make run                   # starts the core systems (:8100) and the assistant (:8000)
```

Open http://127.0.0.1:8000 and pick a demo customer. Demo data is generated for today's date on start-up, so "this month's EMI" always means this month. Without `make`: `python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m scripts.run`.

Deep link for demos: `http://127.0.0.1:8000/?customer=CUST1002&workspace=loan_servicing`.

## Using an AI coding agent

The repo is set up for AI coding tools. [AGENTS.md](AGENTS.md) (read by Codex, Cursor, GitHub Copilot and most agents) and [CLAUDE.md](CLAUDE.md) explain how to set up, run, verify headlessly through the API, and change the code safely. In Claude Code these skills are available:

| Skill | What it does |
|---|---|
| `/run-demo` | Sets up, starts, verifies with the real model and walks through the demo journeys |
| `/tour` | Guided architecture walkthrough with file references |
| `/chaos-demo` | Takes a backend system down and shows how the assistant copes |
| `/add-action` | Adds a customer-confirmed action with OTP, idempotency and audit |
| `/add-workflow` | Adds a new use-case pack (collections, insurance...) on the same platform |

For example: open the repo in Claude Code and type `/run-demo`, or ask any agent "set this project up and show me what it does".

## Demo journeys

| Customer | Workspace | Try | What it shows |
|---|---|---|---|
| Priya Sharma | Loan servicing | "My EMI went up this month. Why?" then "If I prepay ₹50,000 today, how much interest do I save?" then "Would ₹20,000 extra reduce my tenure?" | Cause found in the loan record (benchmark rate reset), EMI recomputed independently, when she was notified, the tenure-extension alternative; prepayment options with exact interest saved; context carried across turns |
| Rahul Verma | Loan servicing | "Show the payments that failed in the last six months and explain what happened." then "Get the bank's one reversed." | Three failures with three different causes; policy decides only one charge is reversible; dispute raised and charge reversed across Payments, Service Ops and CRM after confirmation |
| Rahul Verma | Loan servicing | "Can I foreclose without a penalty?" then "What are my options?" | Fixed-rate foreclosure charge, exact payoff amount, and whether waiting for the cheaper charge slab saves money once the extra interest is counted |
| Priya Sharma | Loan servicing | "Go ahead with the ₹50,000 prepayment." | OTP-confirmed action; the OTP is shown in demo mode and never visible to the model |
| Ananya Iyer | Loan servicing | "Can I prepay my fixed loan?" | Lock-in rule blocks it and says exactly when it opens; she has two loans, so ambiguity is handled |
| Ananya Iyer | Card servicing | "Why was my card declined in Dubai?" then "Turn it on." | Second workflow on the same platform: decline explained, card control changed with OTP |
| Ananya Iyer | Card servicing | "Can I convert my Skyline Electronics purchase into EMIs?" | Card EMI options computed with the same amortization engine, including fees and total cost |

**Voice:** click the microphone next to the send button (Chrome works best; headphones avoid echo). Try the interruption script: ask "What's my next EMI?", talk over the answer with "Actually, check my last payment first", then "Okay, would paying ₹20,000 extra reduce my tenure?". Speech-to-text uses Groq's free Whisper model. Speech output uses Piper, an open-source voice that runs inside the server (free, no rate limits, about 0.2 s per sentence); `make setup` downloads it, or run `make voices`. Without it the browser's built-in voice is used.

Open **Ops** (top right) to set any system to slow, flaky or down and ask again: the assistant answers what it can and says what it couldn't check. The Ops panel also shows p50/p95 latency and a waterfall of the last turn.

## Project layout

```
app/
  platform/        reusable runtime: orchestrator, connectors, policy engine, actions, sessions, telemetry
  domains/
    common/        tools every workspace shares (profile, notices, service requests, policy lookup)
    lending/       loan servicing tools, rules and actions
    cards/         card servicing tools and actions (the second workflow)
  web/             customer app (plain HTML/CSS/JS)
  main.py          HTTP API and streaming chat endpoint
config/
  usecases/        one YAML pack per workflow: tools, policies, instructions, panels
  systems.yaml     latency/failure profiles and client timeouts, retries, caching per system
finlib/            money, dates and amortization shared by the core systems and the assistant
mock_systems/      simulated CRM, Loan Management, Payments, Policy Knowledge, Service Ops, Cards
  policies/        versioned product terms (fictional) served by the Policy Knowledge system
scripts/           launcher and latency benchmark
tests/             unit and integration tests (no LLM calls)
```

## Tests and benchmarks

```bash
make test                                        # unit, integration and conversation-loop tests; no API calls
.venv/bin/python -m scripts.bench --runs 2       # scripted journeys through the real chat API (app must be running)
.venv/bin/python -m scripts.bench --platform-load 100   # no-LLM load test of tools + core systems
```

## Configuration

Settings live in `.env` (see `.env.example`): model provider and model, fallback models for rate limits, brand and assistant name, a fixed business date for repeatable demos, demo mode (OTP shown on screen), ports. Per-system timeouts, retries, cache TTLs and simulated latency are in `config/systems.yaml`. Workflow behaviour is in `config/usecases/*.yaml`.

All customers, accounts and policies are synthetic.
