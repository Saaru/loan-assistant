---
name: add-action
description: Add a new customer-confirmed action to the loan assistant (for example change EMI date, request a no-objection certificate, block a card), with confirmation, optional OTP, idempotent writes and audit. Use when asked to let the assistant do something new on the customer's behalf.
---

# Add an action

Follow the existing pattern exactly; `raise_charge_dispute` / `execute_dispute` in `app/domains/lending/tools.py` and `convert_to_emi` / `execute_emi_conversion` in `app/domains/cards/tools.py` are the references.

1. **Clarify** the action in one line: what changes, in which system, whether it is high-impact (money or security: needs an OTP) and any eligibility rule.
2. **Backend.** If no endpoint can perform the write, add one to `mock_systems/app.py` under the right system's path prefix. Wrap the write in `idempotent(idempotency_key, create)` and take `idempotency_key: str | None = Header(None)`. Add any table or column to `mock_systems/seed.py` (`SCHEMA` and the generator).
3. **Rule.** If eligibility or fees apply, add a rule with an ID to the policy YAML (`mock_systems/policies/`) with `params` and the exact customer-facing `text`; read it in code with `pol.rule("ID").params`, never hard-code the number.
4. **Executor.** In the domain module, add `@action("<type>", "<Title shown on the card>", requires_otp=<bool>)` with `async def execute_...(ctx, params, key) -> dict`. Every backend POST passes `idempotency_key=f"{key}:<step>"`. Log with `log_interaction(ctx, ...)`. Return a small dict the customer can be told (reference, status, due date).
5. **Proposing tool.** Add `@tool("<name>", "<one-line description>", label="<status line>", input_model=..., kind="action")`. Inside: check ownership (`owned_loan` / `owned_card`), check eligibility and return `{"status": "not_allowed", ...}` if it fails, then `pending = await ctx.actions.propose(ctx, "<type>", params=..., summary=[{"label": ..., "value": ...}], otp_destination=(await profile(ctx))["mobile"])` and `return pending.for_model()`.
6. **Register** the tool name in the pack YAML under `tools:` (`config/usecases/*.yaml`).
7. **Test** in `tests/test_tools.py`: propose, confirm with the demo OTP, confirm again and assert the same result (runs once), and a wrong OTP is rejected if OTP is required. Run `make test`.
8. **Try it** with the running app (restart after Python changes): ask for the action in chat, confirm in the card or by saying yes / reading the OTP.
