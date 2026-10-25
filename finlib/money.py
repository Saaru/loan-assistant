"""Money helpers. All amounts are Decimal internally; floats never touch money math."""

from decimal import ROUND_HALF_UP, Decimal

D = Decimal
PAISE = Decimal("0.01")
RUPEE = Decimal("1")


def money(value) -> Decimal:
    """Round to paise."""
    return Decimal(str(value)).quantize(PAISE, rounding=ROUND_HALF_UP)


def rupees(value) -> Decimal:
    """Round to the nearest whole rupee."""
    return Decimal(str(value)).quantize(RUPEE, rounding=ROUND_HALF_UP)


def pct(part, whole) -> Decimal:
    whole = Decimal(str(whole))
    if whole == 0:
        return Decimal("0")
    return (Decimal(str(part)) * 100 / whole).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _group_indian(digits: str) -> str:
    # 1234567 -> 12,34,567
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def inr(value, paise: bool | None = None) -> str:
    """Format as Indian rupees with lakh/crore grouping, e.g. ₹1,23,456 or ₹1,23,456.50.

    paise=None shows paise only when the amount is not a whole rupee.
    """
    amount = money(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    whole, frac = f"{amount:.2f}".split(".")
    show_paise = paise if paise is not None else frac != "00"
    text = _group_indian(whole)
    if show_paise:
        text += "." + frac
    return f"{sign}₹{text}"
