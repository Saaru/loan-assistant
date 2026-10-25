---
name: add-workflow
description: Add a new use-case pack (workflow) to the loan assistant platform, such as collections, insurance or a savings account, reusing the orchestrator, connectors, policy engine, actions, voice and UI. Use when asked to support a new product or journey.
---

# Add a workflow

The card servicing pack is the template: `config/usecases/card_servicing.yaml`, `app/domains/cards/tools.py`, `mock_systems/policies/credit_card.yaml`, and the `cards` / `card_transactions` renderers in `app/web/app.js`. Nothing in `app/platform/` should need to change.

1. **Scope** with the user in a few lines: the customer questions it must answer, the one or two actions it can take, and which system holds the data.
2. **Data and system.** Reuse an existing simulated system if it fits. Otherwise add tables to `mock_systems/seed.py` (with a story customer or two among CUST1001-1003), read/write routes to `mock_systems/app.py` under a new path prefix, and an entry for that prefix in `config/systems.yaml` (latency, error rate, client timeout, retries).
3. **Policy.** Add `mock_systems/policies/<family>.yaml` with versioned rules (IDs, `params`, exact `text`) and any reference codes.
4. **Domain module.** Create `app/domains/<name>/__init__.py` and `tools.py`: read tools with `@tool` (ownership checks, evidence, `inr()` formatting, maths via `finlib`), actions via `/add-action`, a `@context_provider("<name>")` summarising the customer's products in one line, and `@panel(...)` data providers for the account sidebar.
5. **Pack.** Add `config/usecases/<id>.yaml` with `id`, `name`, `order`, `tagline`, `modules` (include `app.domains.common.tools`), `policy_families`, `context` (`customer` plus yours), `panels`, `effort`, `tools`, short `instructions`, and `suggested_prompts`.
6. **UI.** Add renderers for the new panels to `PANELS` in `app/web/app.js` (follow `cards`). The workspace tab, chat, voice, evidence, action cards and Ops panel need no changes, and `switch_workspace` will route questions to the new pack automatically.
7. **Test.** Add a test like `test_card_workflow_reuses_the_platform` in `tests/test_tools.py`; run `make test`.
8. **Try it.** Restart (`make run`), reset demo data if the seed changed (`POST /api/ops/reset`), and ask a question about the new product from another tab to see the automatic workspace switch.
9. Add the pack to the reuse table in `docs/ARCHITECTURE.md`.
