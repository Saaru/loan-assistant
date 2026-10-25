"""Generates the synthetic data behind the simulated core systems.

Three hand-designed demo customers carry the stories the demo needs; a couple of
hundred background customers are generated procedurally for realism and load
tests. Everything is deterministic for a given business date.

    python -m mock_systems.seed            # seed for today
    LA_BUSINESS_DATE=2026-10-25 python -m mock_systems.seed
"""

import json
import os
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from finlib import amortization as am
from finlib.dates import add_months, business_date, last_due_on_or_before
from finlib.money import money

DATA_DIR = Path(os.getenv("LA_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "core.db"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE customers (
  customer_id TEXT PRIMARY KEY, full_name TEXT, dob TEXT, phone TEXT, email TEXT, city TEXT,
  segment TEXT, kyc_status TEXT, preferred_language TEXT, customer_since TEXT, is_demo INTEGER DEFAULT 0,
  demo_story TEXT);
CREATE TABLE interactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT, ts TEXT, channel TEXT, type TEXT, summary TEXT);
CREATE TABLE benchmark_rates (benchmark TEXT, effective_from TEXT, rate TEXT);
CREATE TABLE loans (
  loan_id TEXT PRIMARY KEY, customer_id TEXT, product_code TEXT, product_name TEXT, rate_type TEXT,
  benchmark TEXT, spread TEXT, rate_at_disbursal TEXT, current_rate TEXT, principal TEXT,
  disbursal_date TEXT, tenure_months INTEGER, first_due_date TEXT, maturity_date TEXT, emi TEXT,
  status TEXT, mandate_status TEXT, mandate_bank TEXT, mandate_ref TEXT, mandate_since TEXT);
CREATE TABLE loan_schedule (
  loan_id TEXT, inst_no INTEGER, due_date TEXT, opening TEXT, rate TEXT, emi TEXT, interest TEXT,
  principal TEXT, closing TEXT, status TEXT, paid_date TEXT, PRIMARY KEY (loan_id, inst_no));
CREATE TABLE loan_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, loan_id TEXT, event_date TEXT, type TEXT, details TEXT);
CREATE TABLE payments (
  payment_id TEXT PRIMARY KEY, loan_id TEXT, customer_id TEXT, inst_no INTEGER, due_date TEXT,
  attempt_date TEXT, amount TEXT, method TEXT, status TEXT, reason_code TEXT, bank_reference TEXT);
CREATE TABLE charges (
  charge_id TEXT PRIMARY KEY, loan_id TEXT, payment_id TEXT, charge_date TEXT, type TEXT, amount TEXT,
  gst TEXT, total TEXT, status TEXT, settled_date TEXT, policy_rule TEXT, policy_version TEXT);
CREATE TABLE service_requests (
  request_id TEXT PRIMARY KEY, customer_id TEXT, product_ref TEXT, type TEXT, status TEXT, priority TEXT,
  created_at TEXT, updated_at TEXT, sla_due TEXT, summary TEXT, details TEXT, resolution TEXT,
  idempotency_key TEXT UNIQUE, channel TEXT);
CREATE TABLE cards (
  card_id TEXT PRIMARY KEY, customer_id TEXT, product_name TEXT, last4 TEXT, credit_limit TEXT,
  statement_balance TEXT, min_due TEXT, statement_date TEXT, due_date TEXT, intl_enabled INTEGER,
  status TEXT);
CREATE TABLE card_transactions (
  txn_id TEXT PRIMARY KEY, card_id TEXT, ts TEXT, merchant TEXT, category TEXT, amount TEXT,
  orig_amount TEXT, orig_currency TEXT, country TEXT, status TEXT, decline_code TEXT,
  emi_plan_id TEXT, dispute_ref TEXT);
CREATE TABLE card_emi_plans (
  plan_id TEXT PRIMARY KEY, card_id TEXT, txn_id TEXT, principal TEXT, tenure_months INTEGER,
  annual_rate TEXT, emi TEXT, processing_fee TEXT, gst TEXT, created_at TEXT);
CREATE TABLE idempotency (key TEXT PRIMARY KEY, response TEXT, created_at TEXT);
CREATE INDEX idx_sched ON loan_schedule (loan_id, due_date);
CREATE INDEX idx_pay ON payments (loan_id, attempt_date);
CREATE INDEX idx_txn ON card_transactions (card_id, ts);
CREATE INDEX idx_loans_customer ON loans (customer_id);
CREATE INDEX idx_charges ON charges (loan_id);
CREATE INDEX idx_events ON loan_events (loan_id);
CREATE INDEX idx_requests ON service_requests (customer_id);
CREATE INDEX idx_cards_customer ON cards (customer_id);
CREATE INDEX idx_interactions ON interactions (customer_id, ts);
"""

FIRST_NAMES = ["Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Krishna", "Ishaan", "Rohan",
               "Kavya", "Diya", "Saanvi", "Aadhya", "Isha", "Meera", "Neha", "Pooja", "Riya", "Sneha",
               "Karthik", "Manish", "Nikhil", "Pranav", "Siddharth", "Tanvi", "Shreya", "Lakshmi", "Farhan", "Zoya"]
LAST_NAMES = ["Patel", "Reddy", "Nair", "Gupta", "Singh", "Kumar", "Joshi", "Mehta", "Rao", "Das",
              "Bose", "Kulkarni", "Menon", "Chopra", "Banerjee", "Pillai", "Shetty", "Khan", "Desai", "Agarwal"]
CITIES = ["Mumbai", "Pune", "Ahmedabad", "Bengaluru", "Hyderabad", "Chennai", "Delhi", "Jaipur", "Kolkata", "Surat"]
BANKS = ["Coastal Bank", "Unity Bank", "Saraswat Co-op Bank", "Pioneer Bank", "Metro Bank"]
FILLER_MERCHANTS = [("QuickMart Groceries", "GROCERY", 600, 4200), ("Bluebell Cafe", "DINING", 250, 1800),
                    ("CityRide Cabs", "TRAVEL", 150, 900), ("Metro Fuel Station", "FUEL", 800, 3500),
                    ("PageTurn Books", "SHOPPING", 300, 1500), ("FitZone Gym", "HEALTH", 1500, 3000),
                    ("UrbanWear Apparel", "SHOPPING", 1200, 6500), ("Sunrise Pharmacy", "HEALTH", 200, 1600)]

NWBR = "NWBR"


@dataclass
class Payment:
    months_ago: int
    reason: str
    recovered_after_days: int
    recovery_method: str
    charge_status: str  # COLLECTED or OUTSTANDING
    represented: bool = False


@dataclass
class LoanSpec:
    loan_id: str
    customer_id: str
    product_name: str
    rate_type: str
    principal: int
    tenure: int
    disbursal: date
    spread: Decimal | None = None
    fixed_rate: Decimal | None = None
    failures: list[Payment] = field(default_factory=list)
    mandate_inactive_from_months_ago: int | None = None


class Seeder:
    def __init__(self, as_of: date):
        self.as_of = as_of
        self.last_due = last_due_on_or_before(as_of)
        self.rng = random.Random(20261025)
        self.con: sqlite3.Connection | None = None
        # Benchmark history: one revision ~55 days before the latest EMI, so the
        # hero floating loan picks it up at its most recent reset.
        self.priya_reset = self.last_due - timedelta(days=15)
        self.nwbr_revision = self.last_due - timedelta(days=55)
        self.benchmarks = [(add_months(as_of, -48), Decimal("8.50")), (self.nwbr_revision, Decimal("9.25"))]
        self.counters = {"sr": 104200, "int": 0}

    # ---- helpers -------------------------------------------------------
    def nwbr_on(self, d: date) -> Decimal:
        rate = self.benchmarks[0][1]
        for eff, value in self.benchmarks:
            if eff <= d:
                rate = value
        return rate

    def ts(self, d: date, hour: int = 10, minute: int = 0) -> str:
        return datetime.combine(d, time(hour, minute)).isoformat(timespec="seconds")

    def interaction(self, customer_id: str, d: date, channel: str, type_: str, summary: str, hour: int = 10):
        self.con.execute("INSERT INTO interactions (customer_id, ts, channel, type, summary) VALUES (?,?,?,?,?)",
                         (customer_id, self.ts(d, hour), channel, type_, summary))

    def customer(self, cid, name, dob, phone, email, city, segment, since, is_demo=0, story=None):
        self.con.execute("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (cid, name, dob, phone, email, city, segment, "VERIFIED", "en", since.isoformat(), is_demo, story))

    # ---- loans ---------------------------------------------------------
    def build_loan(self, spec: LoanSpec):
        first_due = add_months(date(spec.disbursal.year, spec.disbursal.month, 5), 1)
        if spec.rate_type == "floating":
            rate = self.nwbr_on(spec.disbursal) + spec.spread
        else:
            rate = spec.fixed_rate
        rate_at_disbursal = rate
        current_emi = am.emi(spec.principal, rate, spec.tenure)
        balance = money(spec.principal)
        next_reset = add_months(spec.disbursal, 3)
        events = [(spec.disbursal, "DISBURSAL", {"amount": str(money(spec.principal)), "rate": str(rate),
                                                 "emi": str(current_emi), "tenure_months": spec.tenure})]
        rows = []
        r_month_rate = rate
        for n in range(1, spec.tenure + 1):
            due = add_months(first_due, n - 1)
            while spec.rate_type == "floating" and next_reset <= due and next_reset <= self.as_of:
                new_rate = self.nwbr_on(next_reset) + spec.spread
                if new_rate != r_month_rate:
                    remaining = spec.tenure - (n - 1)
                    new_emi = am.emi(balance, new_rate, remaining)
                    events.append((next_reset, "RATE_RESET", {
                        "reset_date": next_reset.isoformat(), "benchmark": NWBR,
                        "old_benchmark_rate": str(self.nwbr_on(next_reset - timedelta(days=92))),
                        "new_benchmark_rate": str(self.nwbr_on(next_reset)), "spread": str(spec.spread),
                        "old_rate": str(r_month_rate), "new_rate": str(new_rate),
                        "outstanding_at_reset": str(balance), "remaining_instalments": remaining,
                        "old_emi": str(current_emi), "new_emi": str(new_emi),
                        "first_revised_due_date": due.isoformat(), "first_revised_instalment": n,
                        "treatment": "revise_emi_keep_maturity"}))
                    r_month_rate, current_emi = new_rate, new_emi
                next_reset = add_months(next_reset, 3)
            interest = money(balance * am.monthly_rate(r_month_rate))
            payment = current_emi if (balance + interest > current_emi and n < spec.tenure) else balance + interest
            principal_part = payment - interest
            closing = balance - principal_part
            rows.append([spec.loan_id, n, due.isoformat(), str(balance), str(r_month_rate), str(payment),
                         str(interest), str(principal_part), str(closing), "PAID" if due <= self.as_of else "UPCOMING", None])
            balance = closing
            if balance <= 0:
                break

        maturity = date.fromisoformat(rows[-1][2])
        mandate_status, mandate_since = "ACTIVE", spec.disbursal
        if spec.mandate_inactive_from_months_ago is not None:
            mandate_status = "INACTIVE"
            mandate_since = add_months(self.last_due, -spec.mandate_inactive_from_months_ago) - timedelta(days=4)
            events.append((mandate_since, "MANDATE_STATUS", {"status": "INACTIVE",
                           "reason": "Mandate cancelled by the customer's bank (account closed)"}))
        bank = self.rng.choice(BANKS)
        self.con.execute("INSERT INTO loans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            spec.loan_id, spec.customer_id, "PL-FLT" if spec.rate_type == "floating" else "PL-FIX",
            spec.product_name, spec.rate_type, NWBR if spec.rate_type == "floating" else None,
            str(spec.spread) if spec.spread is not None else None, str(rate_at_disbursal), str(r_month_rate),
            str(money(spec.principal)), spec.disbursal.isoformat(), spec.tenure, first_due.isoformat(),
            maturity.isoformat(), str(current_emi), "ACTIVE", mandate_status, bank,
            f"NACH{spec.loan_id[2:]}{self.rng.randint(1000, 9999)}", mandate_since.isoformat()))
        for d, type_, details in events:
            self.con.execute("INSERT INTO loan_events (loan_id, event_date, type, details) VALUES (?,?,?,?)",
                             (spec.loan_id, d.isoformat(), type_, json.dumps(details)))
        self.build_payments(spec, rows, bank)
        self.con.executemany("INSERT INTO loan_schedule VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        return rows, events

    def build_payments(self, spec: LoanSpec, rows: list, bank: str):
        failures = {add_months(self.last_due, -f.months_ago).isoformat(): f for f in spec.failures}
        mandate_cutoff = None
        if spec.mandate_inactive_from_months_ago is not None:
            mandate_cutoff = add_months(self.last_due, -spec.mandate_inactive_from_months_ago)
        for row in rows:
            if row[9] != "PAID":
                continue
            n, due = row[1], date.fromisoformat(row[2])
            amount = row[5]
            pid = f"PAY{spec.loan_id[2:]}{n:03d}"
            failure = failures.get(row[2])
            if failure:
                self.payment(f"{pid}A", spec, n, due, due, amount, "NACH", "FAILED", failure.reason)
                recovered = due + timedelta(days=failure.recovered_after_days)
                self.payment(f"{pid}B", spec, n, due, recovered, amount, failure.recovery_method, "SUCCESS", None)
                row[10] = recovered.isoformat()
                self.bounce_charge(spec, f"{pid}A", due, failure.charge_status)
                self.interaction(spec.customer_id, due, "SMS", "EMI_BOUNCE_ALERT",
                                 f"EMI of Rs {amount} for loan {spec.loan_id} could not be debited ({failure.reason}).")
            elif mandate_cutoff and due > mandate_cutoff:
                paid_on = due - timedelta(days=1)
                self.payment(f"{pid}A", spec, n, due, paid_on, amount, "NETBANKING", "SUCCESS", None)
                row[10] = paid_on.isoformat()
            else:
                self.payment(f"{pid}A", spec, n, due, due, amount, "NACH", "SUCCESS", None)
                row[10] = due.isoformat()

    def payment(self, pid, spec, n, due, attempt, amount, method, status, reason):
        self.con.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            pid, spec.loan_id, spec.customer_id, n, due.isoformat(), attempt.isoformat(), amount, method,
            status, reason, f"{method[:3]}{self.rng.randint(10**9, 10**10 - 1)}"))

    def bounce_charge(self, spec, payment_id, d: date, status: str):
        version, base = ("2026.1", Decimal(500)) if d >= date(2026, 4, 1) else ("2025.1", Decimal(450))
        gst = money(base * Decimal("0.18"))
        settled = add_months(d, 1).isoformat() if status == "COLLECTED" else None
        self.con.execute("INSERT INTO charges VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            f"CHG{payment_id[3:]}", spec.loan_id, payment_id, d.isoformat(), "BOUNCE_CHARGE", str(money(base)),
            str(gst), str(money(base + gst)), status, settled, "BC-1", version))

    # ---- cards ---------------------------------------------------------
    def card(self, card_id, cid, product, last4, limit, intl=False):
        statement_date = date(self.as_of.year, self.as_of.month, 15)
        if statement_date > self.as_of:
            statement_date = add_months(statement_date, -1)
        due_date = statement_date + timedelta(days=20)
        self.con.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            card_id, cid, product, last4, str(money(limit)), "0", "0", statement_date.isoformat(),
            due_date.isoformat(), 1 if intl else 0, "ACTIVE"))
        return statement_date

    def txn(self, txn_id, card_id, d: date, merchant, category, amount, status="APPROVED", decline=None,
            orig_amount=None, currency="INR", country="IN", hour=None):
        hour = hour if hour is not None else self.rng.randint(9, 21)
        self.con.execute("INSERT INTO card_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            txn_id, card_id, self.ts(d, hour, self.rng.randint(0, 59)), merchant, category, str(money(amount)),
            str(money(orig_amount)) if orig_amount else None, currency, country, status, decline, None, None))

    def filler_txns(self, card_id, prefix, count):
        for i in range(count):
            merchant, cat, lo, hi = self.rng.choice(FILLER_MERCHANTS)
            d = self.as_of - timedelta(days=self.rng.randint(1, 50))
            self.txn(f"{prefix}{i:03d}", card_id, d, merchant, cat, self.rng.randint(lo, hi))

    def finalize_cards(self):
        for (card_id, limit, sdate) in self.con.execute(
                "SELECT card_id, credit_limit, statement_date FROM cards").fetchall():
            start = add_months(date.fromisoformat(sdate), -1).isoformat()
            total = self.con.execute(
                "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) FROM card_transactions WHERE card_id=? AND status='APPROVED' "
                "AND ts >= ? AND ts < ?", (card_id, start, sdate)).fetchone()[0]
            stmt = money(total)
            min_due = max(money(stmt * Decimal("0.05")), Decimal(200)) if stmt > 0 else Decimal(0)
            self.con.execute("UPDATE cards SET statement_balance=?, min_due=? WHERE card_id=?",
                             (str(stmt), str(min(min_due, stmt)), card_id))

    # ---- demo customers ------------------------------------------------
    def heroes(self):
        a = self.as_of
        # Priya: floating loan whose rate reset just raised her EMI; card with an unrecognised subscription.
        self.customer("CUST1001", "Priya Sharma", "1991-03-14", "+919820145621", "priya.sharma@example.com", "Mumbai",
                      "Salaried", add_months(a, -40), 1,
                      "Floating-rate loan; EMI went up this month after a benchmark reset. Card has an unrecognised subscription charge.")
        disb = add_months(self.priya_reset, -15)
        self.build_loan(LoanSpec("LN10001", "CUST1001", "Personal Loan - Floating", "floating", 800000, 48, disb,
                                 spread=Decimal("3.25")))
        old_emi_row = self.con.execute(
            "SELECT details FROM loan_events WHERE loan_id='LN10001' AND type='RATE_RESET' ORDER BY event_date DESC").fetchone()
        details = json.loads(old_emi_row[0])
        notice = self.priya_reset + timedelta(days=1)
        for ch in ("SMS", "EMAIL"):
            self.interaction("CUST1001", notice, ch, "RATE_REVISION_NOTICE",
                             f"Loan LN10001: interest rate revised from {details['old_rate']}% to {details['new_rate']}% "
                             f"after NWBR revision. EMI changes from Rs {details['old_emi']} to Rs {details['new_emi']} "
                             f"from {details['first_revised_due_date']}.")
        self.interaction("CUST1001", add_months(a, -4), "APP", "SERVICE_REQUEST", "Requested address update; completed.")
        self.con.execute("INSERT INTO service_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "SR104101", "CUST1001", None, "ADDRESS_UPDATE", "CLOSED", "NORMAL", self.ts(add_months(a, -4)),
            self.ts(add_months(a, -4) + timedelta(days=2)), (add_months(a, -4) + timedelta(days=3)).isoformat(),
            "Update communication address", "{}", "Address updated", None, "app"))
        self.card("CC1001", "CUST1001", "Northwind Rewards Card", "9023", 150000)
        self.filler_txns("CC1001", "TXP", 22)
        self.txn("TXP900", "CC1001", a - timedelta(days=39), "Streamly Premium", "SUBSCRIPTION", 1499, hour=3)
        self.txn("TXP901", "CC1001", a - timedelta(days=9), "Streamly Premium", "SUBSCRIPTION", 1499, hour=3)

        # Rahul: fixed loan with three failed auto-debits for three different reasons; mandate now inactive.
        self.customer("CUST1002", "Rahul Verma", "1987-11-02", "+919811307742", "rahul.verma@example.com", "Pune",
                      "Self-employed", add_months(a, -30), 1,
                      "Fixed-rate loan; three failed EMIs in six months (one was a bank technical decline). Mandate inactive. Foreclosure charge drops soon.")
        self.build_loan(LoanSpec(
            "LN10002", "CUST1002", "Personal Loan - Fixed", "fixed", 400000, 36,
            add_months(self.last_due, -22) + timedelta(days=10), fixed_rate=Decimal("13.50"),
            failures=[Payment(5, "INSUFFICIENT_FUNDS", 3, "UPI", "COLLECTED"),
                      Payment(2, "TECHNICAL_DECLINE", 2, "NACH", "COLLECTED", represented=True),
                      Payment(1, "MANDATE_INACTIVE", 6, "NETBANKING", "OUTSTANDING")],
            mandate_inactive_from_months_ago=1))
        self.interaction("CUST1002", add_months(self.last_due, -1) - timedelta(days=3), "SMS", "MANDATE_ALERT",
                         "Your NACH mandate for loan LN10002 was cancelled by your bank. Please register a new mandate.")

        # Ananya: two loans (one still in lock-in) and a card with a declined overseas payment.
        self.customer("CUST1003", "Ananya Iyer", "1995-07-22", "+919900812233", "ananya.iyer@example.com", "Bengaluru",
                      "Salaried", add_months(a, -14), 1,
                      "Two loans, the fixed one still in lock-in. Card declined abroad (international usage off); large purchase eligible for EMI.")
        flt_disb = add_months(self.nwbr_revision - timedelta(days=10), -3)
        self.build_loan(LoanSpec("LN10003", "CUST1003", "Personal Loan - Floating", "floating", 250000, 24, flt_disb,
                                 spread=Decimal("3.75")))
        self.build_loan(LoanSpec("LN10004", "CUST1003", "Personal Loan - Fixed", "fixed", 150000, 24,
                                 add_months(self.last_due, -4) + timedelta(days=10), fixed_rate=Decimal("14.50")))
        self.card("CC1003", "CUST1003", "Northwind Platinum Card", "4417", 200000)
        self.filler_txns("CC1003", "TXA", 18)
        self.txn("TXA900", "CC1003", a - timedelta(days=12), "Skyline Electronics", "ELECTRONICS", 48999, hour=18)
        self.txn("TXA901", "CC1003", a - timedelta(days=3), "Harbor View Hotel, Dubai", "TRAVEL", 37620,
                 status="DECLINED", decline="INTL_USAGE_DISABLED", orig_amount=1650, currency="AED", country="AE", hour=22)

    # ---- background customers -----------------------------------------
    def background(self, count=200):
        for i in range(count):
            cid = f"CUST{2001 + i}"
            name = f"{self.rng.choice(FIRST_NAMES)} {self.rng.choice(LAST_NAMES)}"
            self.customer(cid, name, f"19{self.rng.randint(70, 99)}-0{self.rng.randint(1, 9)}-1{self.rng.randint(0, 9)}",
                          f"+9198{self.rng.randint(10**7, 10**8 - 1)}", f"{name.lower().replace(' ', '.')}{i}@example.com",
                          self.rng.choice(CITIES), self.rng.choice(["Salaried", "Self-employed"]),
                          add_months(self.as_of, -self.rng.randint(6, 60)))
            tenure = self.rng.choice([12, 24, 36, 48, 60])
            months_ago = self.rng.randint(2, tenure - 1)
            floating = self.rng.random() < 0.7
            failures = []
            for m in range(1, min(months_ago, 12)):
                if self.rng.random() < 0.05:
                    failures.append(Payment(m, self.rng.choices(
                        ["INSUFFICIENT_FUNDS", "TECHNICAL_DECLINE", "MANDATE_INACTIVE", "ACCOUNT_FROZEN"],
                        weights=[60, 20, 10, 10])[0], self.rng.randint(2, 6), "UPI", "COLLECTED"))
            self.build_loan(LoanSpec(
                f"LN{20001 + i}", cid, "Personal Loan - Floating" if floating else "Personal Loan - Fixed",
                "floating" if floating else "fixed", self.rng.randrange(100000, 1000001, 25000), tenure,
                add_months(self.last_due, -months_ago) + timedelta(days=self.rng.randint(1, 20)),
                spread=Decimal(str(self.rng.choice(["2.75", "3.25", "3.75", "4.25"]))) if floating else None,
                fixed_rate=None if floating else Decimal(str(self.rng.choice(["12.50", "13.50", "14.50", "15.50"]))),
                failures=failures))
            if self.rng.random() < 0.5:
                card_id = f"CC{2001 + i}"
                self.card(card_id, cid, "Northwind Rewards Card", f"{self.rng.randint(1000, 9999)}",
                          self.rng.choice([50000, 100000, 150000, 250000]))
                self.filler_txns(card_id, f"TX{2001 + i}", self.rng.randint(5, 20))

    def run(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DB_PATH.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        self.con = sqlite3.connect(tmp)
        self.con.executescript(SCHEMA)
        for eff, rate in self.benchmarks:
            self.con.execute("INSERT INTO benchmark_rates VALUES (?,?,?)", (NWBR, eff.isoformat(), str(rate)))
        self.heroes()
        self.background()
        self.finalize_cards()
        self.con.executemany("INSERT INTO meta VALUES (?,?)", [
            ("as_of", self.as_of.isoformat()), ("generated_at", datetime.now().isoformat(timespec="seconds"))])
        self.con.commit()
        self.con.close()
        tmp.replace(DB_PATH)


def seed(as_of: date | None = None) -> Path:
    Seeder(as_of or business_date()).run()
    return DB_PATH


def is_fresh(as_of: date | None = None) -> bool:
    if not DB_PATH.exists():
        return False
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT value FROM meta WHERE key='as_of'").fetchone()
        return bool(row) and row[0] == (as_of or business_date()).isoformat()
    except sqlite3.Error:
        return False
    finally:
        con.close()


if __name__ == "__main__":
    path = seed()
    con = sqlite3.connect(path)
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("customers", "loans", "loan_schedule", "payments", "charges", "cards", "card_transactions")}
    print(f"Seeded {path} as of {business_date()}: {counts}")
