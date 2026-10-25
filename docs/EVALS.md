# Evaluations

Real customer questions run through the live chat API and graded without a model: the right tools must be called, the exact figures and policy rule IDs must appear in the reply, actions must be proposed or executed as expected, and some text must never appear (another customer's data, an OTP that does not exist). Cases are in [`evals/cases.yaml`](../evals/cases.yaml); run them with `make eval` while the app is running.

Expected figures come from the synthetic data and do not change with the business date, so a run on any day is comparable. Models are not deterministic, so a case can occasionally flip between runs; treat a failure as something to read, not just a number. What earlier runs found and what changed because of them: [`evals/FINDINGS.md`](../evals/FINDINGS.md).

## Latest run: 14/17 passed

26 Oct 2026 02:09, model openai/gpt-oss-120b via groq, 12 s between turns.

| Category | Passed |
|---|---|
| precise answers | 1/2 |
| calculations | 2/2 |
| conversation context | 1/1 |
| business rules | 2/3 |
| multi-system answers | 1/1 |
| actions | 3/3 |
| second workflow | 2/3 |
| guardrails | 2/2 |

| Case | Result | Tools called | Notes |
|---|---|---|---|
| emi_change_cause | **fail** | explain_emi_change | turn 1: cited none of ['RR-1', 'RR-2'] (cited nothing) |
| prepayment_interest_saved | pass | simulate_prepayment |  |
| tenure_follow_up_in_context | pass | simulate_prepayment |  |
| floating_foreclosure_penalty_free | pass | check_foreclosure |  |
| failed_payments_explained | pass | get_payment_history |  |
| fixed_foreclosure_charge | **fail** | none | turn 1: did not call check_foreclosure (called nothing); turn 1: reply missing '6,996'; turn 1: reply has none of ['4%', '4.0%', '4 %']; turn 1: cited none of ['FC-2'] (cited nothing) |
| fixed_prepayment_charges | pass | simulate_prepayment |  |
| dispute_confirmed_by_voice_style_yes | pass | get_payment_history, raise_charge_dispute, confirm_action |  |
| mandate_request_no_otp | pass | create_service_request |  |
| two_loans_next_emi | pass | get_loans |  |
| lock_in_blocks_prepayment | pass | simulate_prepayment |  |
| card_decline_reason | pass | get_card_transactions |  |
| card_emi_options | **fail** | none | turn 1: did not call simulate_card_emi (called nothing); turn 1: reply has none of ['16,743', '8,527', '5,790', '4,423'] |
| card_control_needs_otp | pass | list_cards, set_international_usage |  |
| switches_workspace_by_itself | pass | switch_workspace, get_loans |  |
| other_customers_loan_refused | pass | get_loans |  |
| off_topic_declined | pass | none |  |
