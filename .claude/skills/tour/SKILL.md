---
name: tour
description: Guided walkthrough of this codebase's architecture with file and line references, for reviewers who want to understand how the loan assistant works or where to change something.
---

# Architecture tour

Give a guided tour in this order. For each stop, open the file, show the few lines that matter (with `path:line` references), and explain in two or three sentences why it is built that way. Keep the whole tour under about 15 minutes of reading. Use `docs/ARCHITECTURE.md` for the reasoning; do not invent design history.

1. **The shape** - `README.md` and the diagram in `docs/ARCHITECTURE.md`: browser, assistant service, six simulated systems.
2. **One turn of the loop** - `app/platform/orchestrator.py`: `run_turn` and `_loop`. Show parallel tool execution, the events streamed to the browser, cancellation on interruption, and how `_plan` loads only the active workspace's tools (plus `switch_workspace`).
3. **A tool that answers precisely** - `explain_emi_change` in `app/domains/lending/tools.py`: finds the cause in the loan record, recomputes the EMI with `finlib.amortization.emi`, checks it against the system, and records evidence.
4. **Where the maths and rules live** - `finlib/amortization.py` and `app/domains/lending/rules.py`; rule parameters come from `mock_systems/policies/personal_loan.yaml` (show a versioned rule such as PP-2).
5. **Safe actions** - `app/platform/actions.py`: propose, confirm with OTP (hashed, never in the model's context), execute once with idempotency keys, audit log. Show `raise_charge_dispute` and its executor.
6. **Unreliable backends** - `app/platform/connectors.py` (timeouts, retries, circuit breaker, cache) and `config/systems.yaml`.
7. **Reuse** - `config/usecases/card_servicing.yaml` and `app/domains/cards/tools.py`: a second workflow on the same platform.
8. **Model independence** - `app/platform/llm/`: one interface, Anthropic and OpenAI-compatible providers, fallback across models.
9. **Voice** - `app/web/voice.js` (voice detection, two-step barge-in, sentence-by-sentence speech) and `app/platform/voice.py` (Whisper, Piper, `speakable`).
10. **Evidence** - `tests/` (what is guaranteed) and `docs/PERFORMANCE.md` (what was measured).

Finish by asking what they want to go deeper on, and offer `/add-action` or `/add-workflow` to see how a change is made.
