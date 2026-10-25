"""Loan servicing business rules as pure functions of loan data + policy.

Kept free of I/O so they can be unit tested with fixed inputs and reused by the
assistant tools, the action handlers and the app panels alike.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finlib import amortization as am
from finlib.dates import add_months, fmt, months_between
from finlib.money import inr, money

from ...platform.policy import PolicyDocument


def D(value) -> Decimal:
    return Decimal(str(value))


def iso(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


@dataclass
class PrepaymentEvaluation:
    eligible: bool
    blockers: list[str]
    amount: Decimal
    charge: Decimal
    gst: Decimal
    charge_basis: str
    simulation: dict | None
    default_mode: str

    @property
    def total_charges(self) -> Decimal:
        return self.charge + self.gst


def evaluate_prepayment(loan: dict, amount, pol: PolicyDocument, prepayments_this_fy: int) -> PrepaymentEvaluation:
    rate_type = loan["rate_type"]
    amount = money(amount)
    outstanding = D(loan["outstanding_principal"])
    blockers = []

    min_paid = int(pol.rule("PP-1").params["min_emis_paid"][rate_type])
    if loan["emis_paid"] < min_paid:
        unlock = add_months(iso(loan["first_due_date"]), min_paid - 1)
        blockers.append(f"Lock-in: part-prepayment opens once {min_paid} EMIs are paid; {loan['emis_paid']} are paid "
                        f"so far, so the earliest is after the EMI due on {fmt(unlock)} [PP-1].")
    min_amount = D(pol.rule("PP-2").params["min_amount"])
    if amount < min_amount:
        blockers.append(f"The minimum part-prepayment is {inr(min_amount)} [PP-2].")
    if amount >= outstanding:
        blockers.append(f"{inr(amount)} covers the full outstanding principal of {inr(outstanding)}, which makes it a "
                        f"foreclosure rather than a part-prepayment [FC-3].")
    max_per_fy = int(pol.rule("PP-3").params["max_per_financial_year"])
    if prepayments_this_fy >= max_per_fy:
        blockers.append(f"{prepayments_this_fy} part-prepayments have already been made this financial year; the "
                        f"limit is {max_per_fy} [PP-3].")

    pp4 = pol.rule("PP-4").params
    charge_pct = D(pp4["charge_pct"][rate_type])
    charge = money(amount * charge_pct / 100)
    gst = money(charge * D(pp4["gst_pct"]) / 100)
    basis = ("No charge on floating rate loans [PP-4]" if charge_pct == 0
             else f"{charge_pct}% of the amount prepaid plus {pp4['gst_pct']}% GST [PP-4]")

    simulation = None
    if amount < outstanding and loan.get("next_due_date"):
        simulation = am.simulate_part_prepayment(outstanding, loan["current_rate"], D(loan["emi"]),
                                                 loan["emis_remaining"], amount, iso(loan["next_due_date"]))
    return PrepaymentEvaluation(not blockers, blockers, amount, charge, gst, basis, simulation,
                                pol.rule("PP-5").params["default_mode"])


@dataclass
class ForeclosureQuote:
    eligible: bool
    blockers: list[str]
    outstanding: Decimal
    accrued_interest: Decimal
    unpaid_charges: Decimal
    charge_pct: Decimal
    charge: Decimal
    gst: Decimal
    per_day_interest: Decimal
    months_since_disbursal: int
    next_slab: dict | None

    @property
    def total(self) -> Decimal:
        return self.outstanding + self.accrued_interest + self.unpaid_charges + self.charge + self.gst

    @property
    def penalty_free(self) -> bool:
        return self.charge_pct == 0


def foreclosure_quote(loan: dict, pol: PolicyDocument, today: date, unpaid_charges: Decimal,
                      upcoming: list[dict]) -> ForeclosureQuote:
    rate_type = loan["rate_type"]
    blockers = []
    min_paid = int(pol.rule("FC-1").params["min_emis_paid"][rate_type])
    if loan["emis_paid"] < min_paid:
        unlock = add_months(iso(loan["first_due_date"]), min_paid - 1)
        blockers.append(f"Lock-in: foreclosure opens once {min_paid} EMIs are paid; {loan['emis_paid']} are paid so far, "
                        f"so the earliest is after the EMI due on {fmt(unlock)} [FC-1].")

    outstanding = D(loan["outstanding_principal"])
    rate = D(loan["current_rate"])
    day_count = int(pol.rule("FC-3").params.get("day_count", 365))
    accrued = am.accrued_interest(outstanding, rate, iso(loan["last_emi_due_date"]) or iso(loan["disbursal_date"]),
                                  today, day_count)
    disbursal = iso(loan["disbursal_date"])
    months_since = months_between(disbursal, today)
    fc2 = pol.rule("FC-2").params
    gst_pct = D(fc2["gst_pct"])
    next_slab = None
    if rate_type == "floating":
        charge_pct = D(fc2["floating_pct"])
    else:
        slabs = fc2["fixed_slabs"]
        index = next(i for i, s in enumerate(slabs)
                     if s["up_to_months_since_disbursal"] is None or months_since < s["up_to_months_since_disbursal"])
        charge_pct = D(slabs[index]["pct"])
        if index + 1 < len(slabs) and D(slabs[index + 1]["pct"]) < charge_pct:
            change_date = add_months(disbursal, slabs[index]["up_to_months_since_disbursal"])
            before = [r for r in upcoming if iso(r["due_date"]) < change_date]
            outstanding_then = D(before[-1]["closing"]) if before else outstanding
            interest_meanwhile = sum((D(r["interest"]) for r in before), Decimal(0))
            new_pct = D(slabs[index + 1]["pct"])
            charge_then = money(outstanding_then * new_pct / 100)
            gst_then = money(charge_then * gst_pct / 100)
            charge_now = money(outstanding * charge_pct / 100)
            saving = charge_now + money(charge_now * gst_pct / 100) - charge_then - gst_then
            next_slab = {"from_date": change_date, "pct": new_pct, "emis_before": len(before),
                         "outstanding_then": outstanding_then, "charge_then": charge_then + gst_then,
                         "interest_meanwhile": interest_meanwhile, "charge_saving": saving,
                         "net_benefit": saving - interest_meanwhile}
    charge = money(outstanding * charge_pct / 100)
    gst = money(charge * gst_pct / 100)
    per_day = money(outstanding * rate / 100 / day_count)
    return ForeclosureQuote(not blockers, blockers, outstanding, accrued, unpaid_charges, charge_pct, charge, gst,
                            per_day, months_since, next_slab)


def reversal_eligibility(reason_code: str | None, charge: dict, pol: PolicyDocument, today: date) -> tuple[bool, str]:
    params = pol.rule("BC-3").params
    if charge["status"] == "REVERSED":
        return False, "Already reversed."
    if reason_code not in params["auto_reverse_reason_codes"]:
        return False, "The failure was attributable to the account, so the charge stands under BC-1."
    age = (today - iso(charge["charge_date"])).days
    if age > params["window_days"]:
        return False, f"The {params['window_days']}-day window for disputes has passed [BC-3]."
    return True, f"Failure was not caused by the customer; reversible on request within {params['window_days']} days [BC-3]."
