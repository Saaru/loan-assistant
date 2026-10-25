---
name: run-demo
description: Set up and start the loan assistant, verify it works end to end, and walk through the demo journeys. Use when someone wants to run, try, demo or evaluate this project.
---

# Run the demo

Goal: get the app running locally, prove it works with a real model, and hand the user a short guided demo.

1. **Prerequisites.** Check `python3 --version` is 3.11 or newer. If `.venv` is missing, run `make setup` (installs requirements, creates `.env` from `.env.example`, downloads the ~60 MB local voice).
2. **API key.** Check `.env` has one of `GROQ_API_KEY`, `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` with a value. If none, ask the user for one and mention Groq is free with no card (https://console.groq.com/keys). Write it into `.env` yourself if they paste it, but never echo the key back or commit `.env`.
3. **Start.** If something already answers on `curl -s localhost:8000/api/health`, reuse it. Otherwise run `make run` in the background and poll the health URL until it returns `"ok": true` (allow ~20 s; the first start seeds data and loads the voice model).
4. **Check configuration.** `curl -s localhost:8000/api/config` must show `"llm_configured": true`. Note the model and `voice.tts_engine`.
5. **Run the journeys headlessly** with the API calls in AGENTS.md ("Verify it end to end"). Pause ~8 s between turns (free-tier rate limits). For each turn, collect the reply text from `text_delta` events, the tools from `tool_end`, and `first_token_ms` / `total_ms` from `turn_end`:
   - CUST1001: "My EMI went up this month. Why?" then "If I prepay ₹50,000 today, how much interest would I save?"
   - CUST1002: "Show me the payments that failed in the last six months and explain what happened." then "The one that was the bank's fault, can you get that charge reversed?" then "Yes, go ahead." Then ask "Show my failed payments again" and check the August bounce charge now says "Reversed on ...".
   - CUST1003 with `"usecase":"card_servicing"`: "Why was my card declined in Dubai?" then "What's my next EMI?" (expect a `workspace_switched` event).
6. **Report** a compact table: question, tools used, key figures in the answer, time to first word, total time. Point out that figures come from tools and rule IDs (like [RR-2]) are cited, and anything that looked wrong.
7. **Hand over the UI.** Tell the user to open http://127.0.0.1:8000, pick a customer, open "Sources & calculations" under an answer, try the Ops panel (top right) to set a system to "down", and click the microphone for voice (Chrome; headphones help; talk over the assistant to interrupt).
8. To stop: Ctrl+C in the terminal running `make run`, or kill the background process you started.
