# Architecture, decisions and trade-offs

## The problem in one line

Customers need answers that are specific to their account and provably correct, and they need to get things done across several back-end systems that are slow and sometimes fail, in one conversation. The business needs the next workflow (cards, collections, insurance) to reuse what was built, not start again.

## Shape of the system

```
 Browser (chat, account panels, confirmation cards, Ops panel)
        │  HTTPS: JSON + Server-Sent Events (streamed turn events)
        ▼
 ┌──────────────────────────── Assistant service (app/) ─────────────────────────────┐
 │  API (main.py): session auth, chat stream, action confirm/cancel, panels, ops      │
 │                                                                                    │
 │  Orchestrator (platform/orchestrator.py)                                           │
 │    stream Claude → run tool calls in parallel → feed results back → repeat         │
 │    emits: status, text deltas, action cards, evidence, per-turn latency            │
 │                                                                                    │
 │  Use-case packs (config/usecases/*.yaml) ── pick tools, policies, context, panels  │
 │                                                                                    │
 │  Domain tools           lending/  cards/  common/                                  │
 │  Platform capabilities  connectors · policy engine · action framework · sessions   │
 │                         telemetry · finlib (money, dates, amortization)            │
 └───────────────┬────────────────────────────────────────────────────────────────────┘
                 │  HTTP with per-system timeout, retry, circuit breaker, read cache
                 ▼
  Customer CRM · Loan Management · Payments · Policy Knowledge · Service Ops · Cards
  (mock_systems/: separate service, own latency profile and failure rate per system,
   idempotent writes, chaos switch)
```

## How one turn works

1. The customer's message is appended to the conversation (history is append-only).
2. The orchestrator streams a Claude response with the tools allowed in this workspace. Tool calls are announced to the UI as soon as they start streaming, so the customer sees "Checking your payment history" within the first LLM round.
3. All tool calls from one round run in parallel. Each tool validates its input, reads from the core systems through the connector layer, applies business rules, and returns structured results with display-ready figures. It also records evidence: which systems were read, which policy rules applied (with version), and every calculation's inputs and outputs.
4. Results go back to Claude, which either calls more tools or writes the answer, citing rule IDs like `[PP-4]`.
5. The UI renders the answer with a "Sources & calculations" panel, the tool steps with per-step latency, and the turn's latency (first feedback, first word, complete).

## Key decisions

### 1. Tools and rules, not retrieval, for anything customer-specific

"How much interest would I save by prepaying ₹50,000?" depends on the outstanding principal, current rate, remaining tenure, the product's prepayment rules, and amortization math. Retrieval over documents cannot produce that number reliably, and a language model should not do the arithmetic. So:

- Calculations live in `finlib/` (Decimal arithmetic, explicit rounding conventions) and are unit tested.
- Rules (lock-in, minimum amounts, charges, slabs, reversal eligibility) are pure functions in `app/domains/lending/rules.py` that read their parameters from the policy document.
- The model chooses tools, combines results across turns, and explains. It is told to quote figures exactly as tools return them and to cite only rule IDs that appeared in tool results.

The calculation library is shared with the simulated loan system, so the assistant can recompute a figure independently and say whether it matches the system of record (the EMI change explanation does this).

**Trade-off:** a new kind of question may need a new tool. In practice the tools are composable (the "what are my options?" question is answered from three of them), and `lookup_policy` covers general policy questions. Adding a tool is a typed Python function plus one line in a pack.

### 2. Policy as versioned data with effective dates

The Policy Knowledge system serves product terms as rules with an ID, the exact text to cite, machine-readable parameters, a version and an effective date. Later versions override earlier ones rule by rule (for example the minimum prepayment fell from ₹25,000 to ₹10,000 on 1 April 2026). Rules are resolved for the relevant date, so a charge levied last year is explained with last year's rule. Changing a fee or limit is a policy edit, not a code change.

The assistant caches policy documents for ten minutes and serves the last known version if the Policy system is down, rather than failing the answer.

### 3. The model can propose actions but cannot execute them

Every action (prepayment request, charge dispute, service request, card controls, EMI conversion) goes through one framework (`platform/actions.py`):

1. A tool checks eligibility again and *proposes* the action. The customer sees a confirmation card with a plain summary.
2. High-impact actions need an OTP sent to the registered mobile. The OTP is never put in the model's context, so the model cannot confirm on the customer's behalf; a prompt injection cannot either.
3. On confirmation (button, or the customer typing the OTP in chat) the action runs once. The action ID is the idempotency key for every backend write, so a retry after a timeout, a double click, or a repeated "yes" never duplicates anything. The simulated Service Ops system deliberately drops 5% of write responses after committing, to exercise exactly this.
4. Every step (proposed, OTP failed, confirmed, executed, failed, cancelled) is written to an audit log.

Multi-system actions (a dispute reverses a charge in Payments, records a resolved ticket in Service Ops and logs the interaction in CRM) are a sequence of idempotent steps, so a partial failure is safe to retry.

### 4. Identity comes from the session, never from the model

Tools receive the customer ID from the authenticated session. Any loan or card ID the model passes is checked against the customer's own products, and other customers' accounts are reported as "not on this profile" (a test covers this). Contact details reach the model masked.

### 5. Assume every system is slow or failing sometimes

The connector layer (`platform/connectors.py`) gives every system a timeout, retries with jittered backoff (reads always, writes only with an idempotency key), a circuit breaker, and a short-lived per-conversation read cache, and traces every attempt. A hard per-tool deadline (8 s by default) caps how long any lookup can hold a turn. When something still fails, the tool returns a structured "system unavailable" result and the model answers what it can and says what it couldn't check. The Ops panel can set any system to slow, flaky or down during a demo.

### 6. Responsiveness

- Streaming end to end, so text appears as it is generated.
- Tool calls are surfaced as status lines the moment the model starts calling them, so the customer gets feedback before the answer is ready.
- Independent tool calls run in parallel; reads within a conversation are cached briefly; account panels warm that cache.
- Model effort is set per workflow (low for servicing, where tools do the heavy lifting), and prompt caching is used where the provider supports it.
- Every model call carries the system prompt and tool definitions, so both are kept lean: one-line tool descriptions, compact JSON schemas, and tool results from earlier turns replaced by a short note (the replies that followed already carry what mattered). This cut the input of the first call for "Why did my EMI go up?" from 4,598 to 3,152 tokens, about a third, and is what makes the assistant usable on free-tier rate limits.
- When a model is rate limited, the round moves to the next configured model immediately rather than waiting.
- Each turn records time to first feedback, time to first word, total time, LLM time, tool time and backend time per system.

### 7. Model-agnostic by design

The orchestrator talks to a small provider interface (`platform/llm/`): stream a round, report tool calls, translate tool results. Two implementations cover the field: Claude through the Anthropic SDK (with prompt caching, effort control and server-side refusal fallback), and any OpenAI-compatible API (Groq, Gemini, Ollama, vLLM, OpenRouter) with automatic fallback to another model when one hits a rate limit. The provider is chosen in `.env`. Because the facts and figures come from tools rather than the model, switching models changes the wording of answers far more than their correctness, and the tests run the full conversation loop with a scripted model and no network at all.

**Trade-off:** provider-specific features (Claude's prompt caching controls, effort levels) are used where available but the loop cannot depend on them.

### 8. Reuse through use-case packs

A workflow is a YAML pack (`config/usecases/`) naming its domain module, tools, policy families, the context to load for the system prompt, the panels the app shows, effort, instructions and suggested prompts. The runtime, connectors, policy engine, action framework (with OTP, idempotency and audit), sessions, telemetry, finance library and UI are shared.

Card servicing is the proof. Adding it took:

| New for cards | Reused unchanged |
|---|---|
| `config/usecases/card_servicing.yaml` | Orchestrator, streaming API, sessions |
| `app/domains/cards/tools.py` (reads, rules, 3 actions) | Connector layer with retries, breaker, cache, tracing |
| `mock_systems/policies/credit_card.yaml` (product terms) | Policy engine (versioning, citations, stale fallback) |
| Two account-panel renderers in `app/web/app.js` (card tile, recent transactions) | Action framework: confirmation, OTP, idempotency, audit |
| | Amortization engine for EMI conversion quotes |
| | Web app: chat, voice, evidence, action cards, Ops, latency |

No platform code changed to add it; the only web change was the two panels that draw card data. A collections or insurance workflow would follow the same path.

**One conversation across workflows.** A session holds every workspace but only the active one's tools and instructions are sent to the model, which keeps each call small. The tab sets the active workspace without clearing the conversation. When the customer asks about another workspace's topic, the model calls a built-in `switch_workspace` tool; the orchestrator swaps the toolset mid-turn and the app's tab and panels follow. For example, on the Cards tab "What's my next EMI?" switches to loans and answers from `get_loans`, and "Is the fixed loan still in lock-in?" later in the same conversation switches back and checks `check_foreclosure`.

## Live voice

Voice is another channel into the same orchestrator, tools and actions; only the reply style changes.

```
 mic ─► audio worklet (20 ms frames) ─► voice activity detection ─► WAV ─► POST /api/voice/turn
                                                                            │ Whisper (speech-to-text)
                                                                            ▼
                                       same orchestrator, tools, actions, voice reply style
                                                                            │ streamed events
 speaker ◄─ play in order ◄─ /api/voice/tts per sentence (Piper, local) ◄────────────┘
```

- **Turn-taking.** An energy-based detector with an adaptive noise floor starts an utterance after 120 ms of speech (keeping 300 ms of pre-roll) and ends it after 700 ms of silence. The browser's echo cancellation plus a higher threshold while the assistant is speaking keep its own voice from counting as an interruption.
- **Responsiveness.** The moment the model picks a tool, the app speaks the tool's status line ("Checking your payment history"). If a lookup runs past 3.5 s it says "Still checking, one moment". The reply is spoken sentence by sentence as it streams; audio for later sentences is fetched while earlier ones play.
- **Interruptions and changing intent.** When the customer starts talking over the assistant, playback stops at once, the server-side turn is cancelled, and the conversation history keeps only what had been said, marked as cut off. The next turn tells the model what the customer actually heard, so "Actually, check my last payment first" is handled as a change of mind, not a new conversation.
- **Speech-friendly replies.** A voice style (one to three sentences, no lists or rule IDs) is added to the prompt, and `speakable()` rewrites amounts and dates for speech ("₹1,85,064.28" becomes "1 lakh 85 thousand 64 rupees").
- **Measured.** Speech-to-text time is recorded on the server; the browser reports the time from the end of the customer's speech to the first sound (acknowledgement) and to the first sentence of the answer, which appear per turn and in the Ops panel.

- **Speech output runs locally.** Piper, an open-source text-to-speech model, runs inside the server: about 0.2 s per sentence on a laptop, no API, no limits. Groq's hosted voice was tried first but its free tier allows only a few minutes of speech a day, and falling back to the browser's voice mid-answer mixed two voices. The engine is now fixed at start-up so a conversation keeps one voice; if a sentence ever fails to synthesise it stays on screen rather than switching voices.

**Trade-offs.** A cascaded pipeline (speech-to-text, then the model, then text-to-speech) is slower than a speech-to-speech model but lets voice reuse every tool, rule, action and safeguard unchanged, and works with free models. Energy-based detection is simple and dependency-free but less robust in noisy rooms than a neural detector such as Silero; swapping it in only touches the browser side. Production telephony would move audio over WebRTC or SIP to a media server rather than uploading utterances over HTTP.

## Security controls in this proof of concept

- Session-bound identity; product ownership checked on every tool call.
- Confirmation for every action; OTP for high-impact ones, held as a hash, three attempts, five-minute expiry, never shown to the model.
- Idempotent writes and an append-only audit log of every action event.
- Masked contact details; the model is instructed never to ask for card numbers, CVV, PIN or passwords.
- Tool results are treated as data, not instructions.
- History is append-only; a failed turn is rolled back so the conversation stays consistent.

Production would add: real customer authentication (the app's identity provider token instead of the demo sign-in), OTP via an SMS gateway, secrets in a vault, PII redaction in logs and traces, rate limiting per customer, and data retention rules for transcripts.

## Performance

Measured numbers are in [PERFORMANCE.md](PERFORMANCE.md), produced by `scripts/bench.py`. Two kinds of measurement:

- **Customer journeys through the real chat API** (needs an API key): time to first feedback, first word and full answer per turn, LLM time, and backend latency per system.
- **Platform load without the LLM**: many conversations running realistic tool sequences against the simulated systems at once. With 100 conversations open, backend latency matched the configured profiles and no tool call failed after retries (retries absorbed the injected failures). With 300 open on one laptop, still no tool call failed after retries, but p95 latency rose to several seconds because the load generator and the simulated systems compete for the same machine.

## Path to 1,000 concurrent conversations

- **Stateless service instances.** Sessions (history, pending actions, read cache) move to Redis behind the same `SessionStore` interface; any instance can serve any turn. The service is async throughout, so one instance holds many open conversations that are mostly waiting on the LLM.
- **The LLM is the main constraint.** 1,000 conversations with a turn every 15 to 30 seconds means roughly 30 to 70 turns a second, and with two or three model rounds per turn, on the order of 100 model requests a second. That needs provisioned rate limits, prompt caching (already on), and per-route effort tuning. A smaller model can take latency-critical routes such as voice once quality is measured against an eval set.
- **Protect the core systems.** Per-system concurrency limits and bulkheads in the connector layer, so a slow Payments system cannot exhaust connections needed by CRM. The circuit breakers already shed load from failing systems.
- **Observability.** The per-turn trace maps directly onto OpenTelemetry spans for a real tracing backend, with alerts on p95 time-to-first-feedback and backend error rates.

## What is simulated

| Area | In this repo | In production |
|---|---|---|
| Core systems | One FastAPI service with six routers, SQLite, latency and failure injection | Existing CRM, LMS, payments, policy and ticketing APIs behind the same connector layer |
| Customer data | Three story-driven customers plus 200 generated ones | Real accounts |
| Sign-in | Pick a demo customer | Token from the app's identity provider |
| OTP | Shown on screen in demo mode | SMS gateway |
| Sessions | In memory | Redis |
| Payment link after prepayment | Described in the result | Payment gateway integration |

## Next steps

1. Streaming speech-to-text (partial transcripts while the customer is still talking) and a neural voice detector, to cut a further few hundred milliseconds off each voice turn.
2. An evaluation set of customer questions with expected figures, run in CI, to catch regressions when prompts, tools or models change.
3. Hand-off to a human agent with a conversation summary and the evidence already gathered.
