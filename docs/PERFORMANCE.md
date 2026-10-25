# Performance

Measured 26 Oct 2026 00:06 on a laptop with `python -m scripts.bench --runs 1 --concurrency 1`. Model openai/gpt-oss-120b via groq, effort `low`, simulated core systems with the latency and failure profiles in `config/systems.yaml` (failures and retries included). Free-tier model APIs limit tokens per minute, so a rate-limited round moves to the next configured model.

11 turns across 4 conversations, 0 errors, wall time 107.8 s.

## Per turn (customer's view)

| Metric | p50 | p95 | n |
|---|---|---|---|
| First feedback (status line or text) | 671 ms | 897 ms | 11 |
| First word of the answer | 1.45 s | 2.91 s | 11 |
| Complete answer | 1.50 s | 3.44 s | 11 |
| Time inside the LLM | 1.39 s | 1.90 s | 11 |

LLM rounds served by: `openai/gpt-oss-120b` 11, `openai/gpt-oss-20b` 7, `qwen/qwen3.8-27b` 2.

## By journey turn

| Journey | Turn | Tools | First word | Complete |
|---|---|---|---|---|
| EMI change, prepayment what-if, tenure follow-up | My EMI went up this month. Why? | 1 | 1.19 s | 1.73 s |
| EMI change, prepayment what-if, tenure follow-up | If I make a ₹50,000 prepayment today, how much interest would I save? | 1 | 1.51 s | 1.89 s |
| EMI change, prepayment what-if, tenure follow-up | Okay, and would paying ₹20,000 extra reduce my tenure? | 1 | 1.19 s | 1.40 s |
| Failed payments, explanation, dispute | Show me the payments that failed in the last six months and explain what happened. | 1 | 2.91 s | 3.44 s |
| Failed payments, explanation, dispute | The one that was the bank's fault, can you get that charge reversed? | 1 | 1.45 s | 1.50 s |
| Failed payments, explanation, dispute | (action confirmed) | 0 | 408 ms | 526 ms |
| Foreclosure eligibility and options | Am I eligible for foreclosure without a penalty? | 1 | 2.00 s | 2.62 s |
| Foreclosure eligibility and options | Based on my current loan and the terms, what are my options? | 1 | 1.96 s | 2.47 s |
| Card decline and international usage (second workflow) | Why was my card declined in Dubai? | 1 | 1.45 s | 1.47 s |
| Card decline and international usage (second workflow) | Please turn international usage on. | 1 | 1.14 s | 1.20 s |
| Card decline and international usage (second workflow) | (action confirmed) | 0 | 897 ms | 1.05 s |

## Backend systems (from the connector layer, cache hits excluded)

| System | p50 | p95 | calls | failed attempts |
|---|---|---|---|---|
| cards | 434.0 ms | 434.0 ms | 1 | 0 |
| crm | 61.1 ms | 61.1 ms | 1 | 0 |
| loans | 147.1 ms | 212.9 ms | 4 | 0 |
| payments | 487.6 ms | 1.63 s | 5 | 1 |
| policy | 103.2 ms | 103.2 ms | 2 | 0 |
| service_ops | 311.4 ms | 560.7 ms | 3 | 0 |

Backend read cache hit rate: 43%.

## Tools

| Tool | p50 | p95 | n |
|---|---|---|---|
| check_foreclosure | 1.07 s | 1.07 s | 1 |
| explain_emi_change | 215.5 ms | 215.5 ms | 1 |
| get_card_transactions | 434.7 ms | 434.7 ms | 1 |
| get_payment_history | 1.64 s | 1.64 s | 1 |
| raise_charge_dispute | 258.2 ms | 258.2 ms | 1 |
| set_international_usage | 1.3 ms | 1.3 ms | 1 |
| simulate_prepayment | 312.6 ms | 563.2 ms | 3 |

Prompt cache: 2% of input tokens were served from cache.

## Platform load without the LLM (100 conversations)

`python -m scripts.bench --platform-load 100`: each conversation runs list_loans (since merged into get_loans), explain_emi_change, get_payment_history, simulate_prepayment and check_foreclosure against the simulated core systems, with 1 to 3 s between calls standing in for LLM rounds. Conversations started over 10 s; peak 98 open at once. 500 tool calls in 22.6 s (22 tool calls/s), 0 failed after retries.

| Tool | p50 | p95 | n |
|---|---|---|---|
| check_foreclosure | 209 ms | 632 ms | 100 |
| explain_emi_change | 357 ms | 982 ms | 100 |
| get_payment_history | 706 ms | 1.88 s | 100 |
| list_loans | 188 ms | 551 ms | 100 |
| simulate_prepayment | 352 ms | 1.06 s | 100 |

| System | p50 | p95 | calls | failed attempts |
|---|---|---|---|---|
| crm | 106 ms | 288 ms | 101 | 1 |
| loans | 188 ms | 624 ms | 488 | 13 |
| payments | 481 ms | 1.36 s | 209 | 9 |
| policy | 73 ms | 73 ms | 1 | 0 |
| service_ops | 296 ms | 1.01 s | 107 | 7 |

The same test with 300 conversations (`--platform-load 300`, peak 299 open at once) also finished with 0 tool calls failed after retries, but p95 tool latency rose to 4 to 6 s. The load generator and the simulated systems run on the same laptop, and a separate burst test against the simulated systems alone showed them adding 1 to 2 s of queueing at 300 simultaneous requests (about 15 ms at 50), so the slowdown comes from the test rig rather than the assistant's connector layer.

## Voice

Measured 26 Oct 2026 through the real browser pipeline: headless Chrome with a recorded question played in as the microphone, so voice detection, WAV encoding, streaming and playback run exactly as in a live call. Speech-to-text is Whisper on Groq, the model is gpt-oss-120b on Groq, and speech output is the local Piper voice. Three runs of "What's my next EMI?".

| Step | Result |
|---|---|
| Speech-to-text, every voice turn recorded during development | p50 295 ms, p95 486 ms (n = 93) |
| End of speech to the spoken acknowledgement ("Let me check.") | 0.83 s, 0.91 s, 1.02 s |
| End of speech to the first sentence of the answer | 1.86 s, 2.10 s, 2.14 s |
| Piper synthesis | about 0.2 s per sentence, 0.04 s for the acknowledgement |

"End of speech" is when the detector decides the customer has finished: 800 ms after they stop talking, so the delay a caller feels is that much longer. Streaming speech-to-text (partial transcripts while the customer is still talking) and a neural voice detector are the next steps to cut it.
