import calendar
import os
from datetime import date

EMI_DUE_DAY = 5


def business_date() -> date:
    """The date the whole system treats as 'today'. Override with LA_BUSINESS_DATE=YYYY-MM-DD."""
    override = os.getenv("LA_BUSINESS_DATE")
    return date.fromisoformat(override) if override else date.today()


def add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def months_between(start: date, end: date) -> int:
    """Whole months elapsed from start to end."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return months


def last_due_on_or_before(d: date, due_day: int = EMI_DUE_DAY) -> date:
    if d.day >= due_day:
        return date(d.year, d.month, due_day)
    return add_months(date(d.year, d.month, due_day), -1)


def financial_year_start(d: date) -> date:
    """Indian financial year starts on 1 April."""
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def fmt(d: date) -> str:
    return d.strftime("%d %b %Y")
