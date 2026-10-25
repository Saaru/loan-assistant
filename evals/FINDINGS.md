# What the evaluations found

A log of evaluation runs and what each one led to. The generated results of the latest run are in [docs/EVALS.md](../docs/EVALS.md).

## Run 1: 12/17 passed

- **3 failures were quota, not quality.** The free tier's per-minute token cap rejected some turns. The runner now waits and retries a rate-limited turn once (the failed turn is rolled back by the app, so a retry is safe).
- **Off-topic guardrail failed.** Asked for a poem about cricket, the model wrote one. The system prompt now says to decline anything unrelated in one sentence and not produce it. Passes since.
- **Ambiguous "What's my next EMI?" for a customer with two loans.** The model picked one loan and used the schedule tool. The loan workspace instructions now say: no loan named, call `get_loans` and give each loan's figure. Passes since.

## Run 2: 15/17 passed

- **The EMI-change answer was right but cited no rule ID** in this run, where it had in run 1. Left as a failing check: citation consistency is a real, if small, quality gap to keep measuring.
- **The card EMI answer was correct but did not restate the purchase price.** The check required ₹48,999 in the reply, which is stricter than the job needs; it now requires the EMI figures only.
- **Not caught by any check, found by reading the replies:** for the two-loan question the model added "Total: ₹19,035" by summing the EMIs itself. The sum was right, but the design says the model never does arithmetic. `get_loans` now returns `total_of_next_emis` so a total can be quoted rather than computed, and the case fails if a different total appears.

## Run 3: 14/17 passed

- **Most serious finding so far: an invented figure.** Asked "what's the EMI on my floating-rate loan?" from the card workspace, the model switched workspace correctly but then answered "₹19,016" without calling a loan tool; the real EMI is ₹11,798. Fix in the orchestrator, not the prompt: the model round right after a workspace switch must call a tool (`tool_choice: required` on providers that support it), and the switch result tells the model to look the answer up first. Passed on the next run with `switch_workspace` then `get_loans` and the correct ₹11,798.
- **Wrong citation.** The EMI-change answer cited [PP-2], a prepayment rule that did not apply, though every figure was right.
- **The lock-in answer was correct** ("part-prepayment opens only after 6 EMIs", citing [PP-1]) but did not use the words "lock-in". The check was widened to accept equivalent wording.

## Run 4: 14/17 passed

- The workspace-switch case now passes.
- **Unnecessary clarifying questions.** "Am I eligible for foreclosure without a penalty?" from a customer with one loan, and a card EMI question from a customer with one card, got "Which loan?" and "Which card?". This happened while the free tier was rate limiting, so a smaller fallback model probably answered. The prompt now says to use the only matching product without asking; a targeted recheck of the foreclosure case passed.
- **Citations still missing** from the EMI-change answer. Rather than more prompt text, every tool result now carries a `cite` list of the rule IDs it applied, taken from the evidence it recorded, so the model has the exact IDs in front of it.

These last two changes were made after run 4, with only a targeted recheck because the day's free-tier quota was nearly used up. The next full run will show whether they hold.

## How to use these

Run `make eval` before and after any change to prompts, tools, rules or the model. Read failures rather than just counting them: models are not deterministic, so a single flip can be noise, while a check that fails twice is a regression.
