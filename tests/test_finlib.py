from datetime import date
from decimal import Decimal

from finlib import amortization as am
from finlib.dates import add_months, financial_year_start, last_due_on_or_before, months_between
from finlib.money import inr


def test_emi_matches_standard_formula():
    # 5,00,000 at 12% for 60 months: textbook EMI is 11,122.22, rounded to the rupee.
    assert am.emi(500000, 12, 60) == Decimal("11122")


def test_schedule_closes_exactly_at_tenure():
    emi = am.emi(800000, "11.75", 48)
    rows = am.schedule(800000, "11.75", emi, date(2025, 7, 5), max_months=48)
    assert len(rows) == 48
    assert rows[-1].closing == 0
    assert sum(r.principal for r in rows) == Decimal("800000.00")


def test_prepayment_saves_more_interest_when_tenure_shrinks():
    sim = am.simulate_part_prepayment(Decimal("573824.69"), "12.50", Decimal("21179"), 32, 50000, date(2026, 11, 5))
    assert sim["reduce_tenure"]["instalments_left"] < sim["baseline"]["instalments_left"]
    assert sim["reduce_emi"]["instalments_left"] == sim["baseline"]["instalments_left"] == 32
    assert sim["reduce_tenure"]["interest_saved"] > sim["reduce_emi"]["interest_saved"] > 0


def test_accrued_interest_actual_by_365():
    assert am.accrued_interest(100000, 12, date(2026, 10, 5), date(2026, 10, 25)) == Decimal("657.53")


def test_indian_number_formatting():
    assert inr(123456) == "₹1,23,456"
    assert inr(Decimal("12345678.5")) == "₹1,23,45,678.50"
    assert inr(999) == "₹999"
    assert inr(-590) == "-₹590"


def test_date_helpers():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert months_between(date(2024, 12, 15), date(2026, 10, 25)) == 22
    assert last_due_on_or_before(date(2026, 10, 4)) == date(2026, 9, 5)
    assert financial_year_start(date(2026, 3, 31)) == date(2025, 4, 1)
