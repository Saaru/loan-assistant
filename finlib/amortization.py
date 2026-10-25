"""Reducing-balance amortization used by both the simulated core systems and the assistant.

Conventions (stated in every calculation the assistant shows):
- Monthly rate = annual rate / 12.
- EMI is rounded to the nearest rupee; interest each month is rounded to paise.
- The final instalment is adjusted so the loan closes at exactly zero.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .dates import add_months
from .money import money, rupees

MAX_MONTHS = 600


def monthly_rate(annual_pct) -> Decimal:
    return Decimal(str(annual_pct)) / Decimal(1200)


def emi(principal, annual_pct, months: int) -> Decimal:
    p = Decimal(str(principal))
    if months <= 0 or p <= 0:
        return Decimal(0)
    r = monthly_rate(annual_pct)
    if r == 0:
        return rupees(p / months)
    factor = (1 + r) ** months
    return rupees(p * r * factor / (factor - 1))


@dataclass
class Instalment:
    n: int
    due_date: date
    opening: Decimal
    interest: Decimal
    principal: Decimal
    emi: Decimal
    closing: Decimal

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "due_date": self.due_date.isoformat(),
            "opening": str(self.opening),
            "interest": str(self.interest),
            "principal": str(self.principal),
            "emi": str(self.emi),
            "closing": str(self.closing),
        }


def schedule(principal, annual_pct, emi_amount, first_due: date, max_months: int = MAX_MONTHS) -> list[Instalment]:
    """Project instalments until the balance reaches zero. When max_months is the
    loan's remaining tenure, the last instalment closes the loan (absorbing rounding)."""
    balance = money(principal)
    emi_amount = money(emi_amount)
    r = monthly_rate(annual_pct)
    rows: list[Instalment] = []
    n = 0
    while balance > 0 and n < max_months:
        n += 1
        interest = money(balance * r)
        if emi_amount <= interest:
            raise ValueError("EMI does not cover monthly interest; the loan would never close")
        payment = emi_amount
        if balance + interest <= emi_amount or n == max_months:
            payment = balance + interest
        principal_part = payment - interest
        closing = balance - principal_part
        rows.append(Instalment(n, add_months(first_due, n - 1), balance, interest, principal_part, payment, closing))
        balance = closing
    return rows


def total_interest(rows: list[Instalment]) -> Decimal:
    return sum((row.interest for row in rows), Decimal(0))


def months_to_close(principal, annual_pct, emi_amount) -> int:
    return len(schedule(principal, annual_pct, emi_amount, date(2000, 1, 1)))


def accrued_interest(principal, annual_pct, from_date: date, to_date: date, day_count: int = 365) -> Decimal:
    days = max((to_date - from_date).days, 0)
    return money(Decimal(str(principal)) * Decimal(str(annual_pct)) / 100 * days / day_count)


def simulate_part_prepayment(outstanding, annual_pct, current_emi, remaining_months: int, amount, next_due: date) -> dict:
    """Compare the current plan with a part-prepayment applied before the next EMI.

    Returns both standard options: keep the EMI and shorten the tenure, or keep
    the tenure and lower the EMI.
    """
    outstanding = money(outstanding)
    amount = money(amount)
    # The final instalment absorbs EMI rounding, exactly as the loan system does.
    base = schedule(outstanding, annual_pct, current_emi, next_due, max_months=remaining_months)
    base_interest = total_interest(base)
    new_principal = outstanding - amount

    tenure_rows = schedule(new_principal, annual_pct, current_emi, next_due, max_months=remaining_months)
    reduced_emi = emi(new_principal, annual_pct, remaining_months)
    emi_rows = schedule(new_principal, annual_pct, reduced_emi, next_due, max_months=remaining_months)

    return {
        "baseline": {
            "emi": current_emi,
            "instalments_left": len(base),
            "last_emi_date": base[-1].due_date,
            "interest_remaining": base_interest,
        },
        "reduce_tenure": {
            "emi": money(current_emi),
            "instalments_left": len(tenure_rows),
            "instalments_saved": len(base) - len(tenure_rows),
            "last_emi_date": tenure_rows[-1].due_date,
            "interest_remaining": total_interest(tenure_rows),
            "interest_saved": base_interest - total_interest(tenure_rows),
        },
        "reduce_emi": {
            "emi": reduced_emi,
            "emi_reduction": money(current_emi) - reduced_emi,
            "instalments_left": len(emi_rows),
            "last_emi_date": emi_rows[-1].due_date,
            "interest_remaining": total_interest(emi_rows),
            "interest_saved": base_interest - total_interest(emi_rows),
        },
        "new_principal": new_principal,
    }
