"""Generate sql/seed.sql: realistic FinTrust test data for July to September 2026.

    python scripts/generate_seed.py

The output is deterministic (fixed random seed), so running this again produces
the same file. Every person and account is fictional; emails use example.com
and phone numbers use a reserved range.

Risk scores are calculated with the same rules and numbers the scoring Lambda
uses (see fintrust/config.py DEFAULTS), so the seed data is consistent with the
live system.

Deliberate patterns, used to verify the SQL reports by hand:
  ACC-1007  five HIGH risk transactions inside 30 days  -> must appear in risk_report.sql
  ACC-1005  four HIGH risk transactions, but never more than two in any 30 days
            -> must NOT appear in risk_report.sql (proves the rolling window)
  ACC-1003  thirteen transactions in under an hour      -> HIGH_VELOCITY
  ACC-1011  two HIGH risk transactions                  -> below the report threshold
"""

import random
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fintrust.config import DEFAULTS  # noqa: E402

SAST = timezone(timedelta(hours=2))
OUTPUT = Path(__file__).resolve().parents[1] / "sql" / "seed.sql"

HIGH_AMOUNT_THRESHOLD = Decimal(DEFAULTS["high_amount_threshold"])
HIGH_AMOUNT_POINTS = int(DEFAULTS["high_amount_points"])
VELOCITY_LIMIT = int(DEFAULTS["velocity_limit"])
VELOCITY_WINDOW = timedelta(minutes=int(DEFAULTS["velocity_window_minutes"]))
VELOCITY_POINTS = int(DEFAULTS["velocity_points"])
HOME_COUNTRY = DEFAULTS["home_country"]
CROSS_BORDER_POINTS = int(DEFAULTS["cross_border_points"])
MEDIUM_SCORE = int(DEFAULTS["medium_risk_score"])
HIGH_SCORE = int(DEFAULTS["high_risk_score"])

rng = random.Random(2026)

CUSTOMERS = [
    ("CUST-0001", "Thandiwe Mokoena", "thandiwe.mokoena@example.com", "+27 82 555 0101"),
    ("CUST-0002", "Sipho Dlamini", "sipho.dlamini@example.com", "+27 83 555 0102"),
    ("CUST-0003", "Ayesha Patel", "ayesha.patel@example.com", "+27 84 555 0103"),
    ("CUST-0004", "Johan van der Merwe", "johan.vandermerwe@example.com", "+27 82 555 0104"),
    ("CUST-0005", "Lerato Khumalo", "lerato.khumalo@example.com", "+27 71 555 0105"),
    ("CUST-0006", "Karoo Freight Logistics (Pty) Ltd", "accounts@karoofreight.example.com",
     "+27 21 555 0106"),
    ("CUST-0007", "Nomvula Zulu", "nomvula.zulu@example.com", "+27 72 555 0107"),
    ("CUST-0008", "Michael Naidoo", "michael.naidoo@example.com", "+27 83 555 0108"),
    ("CUST-0009", "Zanele Mahlangu", "zanele.mahlangu@example.com", "+27 76 555 0109"),
    ("CUST-0010", "Pieter Botha", "pieter.botha@example.com", "+27 82 555 0110"),
]

# (account_id, customer_id, type, status, opened, monthly salary or None)
ACCOUNTS = [
    ("ACC-1001", "CUST-0001", "CHEQUE", "ACTIVE", "2019-03-14", 28500),
    ("ACC-1002", "CUST-0001", "SAVINGS", "ACTIVE", "2020-07-01", None),
    ("ACC-1003", "CUST-0002", "CHEQUE", "ACTIVE", "2021-01-20", 19800),
    ("ACC-1004", "CUST-0003", "CHEQUE", "ACTIVE", "2018-11-05", 46200),
    ("ACC-1005", "CUST-0004", "CHEQUE", "ACTIVE", "2015-06-30", 61500),
    ("ACC-1006", "CUST-0005", "CHEQUE", "ACTIVE", "2022-02-11", 23400),
    ("ACC-1007", "CUST-0006", "BUSINESS", "FLAGGED", "2017-09-18", None),
    ("ACC-1008", "CUST-0007", "CHEQUE", "ACTIVE", "2023-05-02", 17250),
    ("ACC-1009", "CUST-0008", "CREDIT", "ACTIVE", "2020-10-09", None),
    ("ACC-1010", "CUST-0008", "CHEQUE", "ACTIVE", "2016-04-22", 38900),
    ("ACC-1011", "CUST-0009", "CHEQUE", "ACTIVE", "2019-08-27", 33700),
    ("ACC-1012", "CUST-0010", "SAVINGS", "CLOSED", "2014-01-15", None),
]

# (merchant, channel, low, high, description)
EVERYDAY = [
    ("Checkers Sandton", "POS", 180, 1850, "Groceries"),
    ("Pick n Pay Rosebank", "POS", 150, 1600, "Groceries"),
    ("Woolworths Food", "POS", 220, 1400, "Groceries"),
    ("Shoprite Soweto", "POS", 90, 950, "Groceries"),
    ("Engen Garage", "POS", 400, 1300, "Fuel"),
    ("Shell Ultra City", "POS", 450, 1250, "Fuel"),
    ("Takealot", "ONLINE", 199, 4500, "Online shopping"),
    ("Uber SA", "MOBILE", 45, 320, "Ride"),
    ("MTN Airtime", "MOBILE", 29, 299, "Airtime and data"),
    ("Vodacom Airtime", "MOBILE", 29, 399, "Airtime and data"),
    ("City Power Prepaid", "ONLINE", 200, 1500, "Prepaid electricity"),
    ("Dis-Chem Pharmacy", "POS", 85, 1200, "Pharmacy"),
    ("Mr Price", "POS", 150, 1800, "Clothing"),
    ("Nando's", "POS", 95, 520, "Restaurant"),
    ("DStv", "EFT", 399, 929, "Subscription"),
    (None, "ATM", 200, 3000, "Cash withdrawal"),
]

transactions = []  # dicts, scored and numbered at the end


def at(month, day, hour, minute, second=0):
    return datetime(2026, month, day, hour, minute, second, tzinfo=SAST)


def add(account, when, amount, tx_type, channel, merchant, country, description):
    transactions.append(
        {
            "account_id": account,
            "tx_date": when,
            "amount": Decimal(str(amount)).quantize(Decimal("0.01")),
            "tx_type": tx_type,
            "channel": channel,
            "merchant_name": merchant,
            "country_code": country,
            "description": description,
        }
    )


def random_time(month):
    last_day = 30 if month == 9 else 31
    return at(month, rng.randint(1, last_day), rng.randint(7, 20), rng.randint(0, 59), rng.randint(0, 59))


# ---- ordinary activity -------------------------------------------------------
for account_id, _customer, account_type, status, _opened, salary in ACCOUNTS:
    if status == "CLOSED":
        add(account_id, at(7, 3, 10, 15), 12400.00, "CREDIT", "EFT", None, "ZA", "Opening balance")
        add(account_id, at(7, 9, 11, 40), 12400.00, "DEBIT", "BRANCH", None, "ZA", "Account closure payout")
        continue

    opening = rng.randint(4000, 30000) if account_type != "BUSINESS" else 310000
    add(account_id, at(7, 1, 8, rng.randint(0, 50)), opening, "CREDIT", "EFT", None, "ZA", "Opening balance")

    for month in (7, 8, 9):
        if salary:
            add(account_id, at(month, 25, 6, rng.randint(0, 30)), salary, "CREDIT", "EFT",
                "Employer payroll", "ZA", "Salary")
        if account_type == "SAVINGS":
            add(account_id, at(month, 26, 9, rng.randint(0, 59)), rng.choice([1500, 2000, 2500]),
                "CREDIT", "EFT", None, "ZA", "Monthly savings transfer")
            continue
        if account_type == "BUSINESS":
            add(account_id, at(month, rng.randint(3, 8), 10, rng.randint(0, 59)),
                rng.choice([186500, 212000, 247300]), "CREDIT", "EFT", "Client settlement", "ZA",
                "Invoice payment received")
            add(account_id, at(month, 28, 14, rng.randint(0, 59)), rng.randint(38000, 46000),
                "DEBIT", "EFT", "Staff payroll run", "ZA", "Salaries")
        for _ in range(rng.randint(5, 7)):
            merchant, channel, low, high, description = rng.choice(EVERYDAY)
            amount = rng.randint(low * 100, high * 100) / 100
            add(account_id, random_time(month), amount, "DEBIT", channel, merchant, "ZA", description)

# ---- small cross-border purchases (LOW risk: cross-border only) --------------
for account_id, month, day, merchant, country, amount in [
    ("ACC-1001", 7, 18, "Amazon.com", "US", 1249.00),
    ("ACC-1004", 8, 2, "Booking.com", "NL", 8450.00),
    ("ACC-1006", 8, 14, "Spotify", "SE", 64.99),
    ("ACC-1008", 9, 3, "AliExpress", "CN", 730.50),
    ("ACC-1010", 7, 22, "Emirates", "AE", 14800.00),
    ("ACC-1010", 9, 11, "Apple.com", "IE", 2199.00),
    ("ACC-1009", 8, 19, "Airbnb", "PT", 9300.00),
]:
    add(account_id, at(month, day, 13, rng.randint(0, 59)), amount, "DEBIT", "ONLINE", merchant,
        country, "International purchase")

# ---- large domestic payments (MEDIUM risk: amount only) ----------------------
for account_id, month, day, merchant, amount, description in [
    ("ACC-1004", 7, 28, "Toyota Fourways", 185000.00, "Vehicle deposit"),
    ("ACC-1010", 8, 27, "Sanlam Investments", 75000.00, "Investment contribution"),
    ("ACC-1001", 9, 26, "Attorneys trust account", 92500.00, "Property transfer costs"),
]:
    add(account_id, at(month, day, 9, 30), 250000.00 if account_id == "ACC-1004" else amount + 20000,
        "CREDIT", "EFT", None, "ZA", "Transfer from own savings")
    add(account_id, at(month, day, 11, 15), amount, "DEBIT", "EFT", merchant, "ZA", description)

# ---- ACC-1007: five large cross-border payments inside 30 days (HIGH) --------
for month, day, merchant, country, amount in [
    (8, 20, "Shenzhen Parts Trading Co", "CN", 78400.00),
    (8, 27, "Dubai Marine Supplies LLC", "AE", 96250.00),
    (9, 2, "Shenzhen Parts Trading Co", "CN", 81900.00),
    (9, 9, "Northgate Shipping Ltd", "GB", 67300.00),
    (9, 15, "Dubai Marine Supplies LLC", "AE", 104500.00),
]:
    add("ACC-1007", at(month, day, 15, rng.randint(0, 59)), amount, "DEBIT", "EFT", merchant, country,
        "Supplier payment")

# ---- ACC-1005: four HIGH risk payments, never more than two in 30 days -------
for month, day, merchant, country, amount in [
    (7, 2, "Lisbon Property Management", "PT", 58000.00),
    (8, 5, "Lisbon Property Management", "PT", 58000.00),
    (9, 8, "Lisbon Property Management", "PT", 58000.00),
    (9, 28, "University of Edinburgh", "GB", 71250.00),
]:
    add("ACC-1005", at(month, day, 10, 5), amount, "DEBIT", "EFT", merchant, country,
        "International transfer")
for month, day in [(7, 1), (8, 4), (9, 7)]:
    add("ACC-1005", at(month, day, 16, 20), 65000.00, "CREDIT", "EFT", None, "ZA",
        "Transfer from investment account")

# ---- ACC-1011: two HIGH risk payments ----------------------------------------
add("ACC-1011", at(7, 15, 9, 10), 120000.00, "CREDIT", "EFT", None, "ZA", "Inheritance payout")
add("ACC-1011", at(7, 16, 12, 45), 54300.00, "DEBIT", "EFT", "Harare Family Support", "ZW",
    "Family remittance")
add("ACC-1011", at(9, 19, 12, 20), 51800.00, "DEBIT", "EFT", "Harare Family Support", "ZW",
    "Family remittance")

# ---- ACC-1003: thirteen transactions in under an hour (velocity) -------------
add("ACC-1003", at(9, 5, 20, 40), 70000.00, "CREDIT", "EFT", None, "ZA", "Loan disbursement")
burst_start = at(9, 5, 21, 2)
for number in range(13):
    when = burst_start + timedelta(minutes=number * 4, seconds=rng.randint(0, 50))
    if number == 11:
        add("ACC-1003", when, 62000.00, "DEBIT", "ONLINE", "Luxury Watches Online", "ZA",
            "Online purchase")
    elif number == 12:
        add("ACC-1003", when, 349.00, "DEBIT", "ONLINE", "GameCodes Direct", "RO", "Online purchase")
    else:
        add("ACC-1003", when, rng.choice([49.00, 99.00, 149.00, 199.00]), "DEBIT", "ONLINE",
            "GameCodes Direct", "ZA", "Online purchase")

# ---- score every transaction with the live rules -----------------------------
transactions.sort(key=lambda t: (t["tx_date"], t["account_id"]))
by_account = {}
for sequence, tx in enumerate(transactions, start=1):
    tx["transaction_id"] = f"TXN-{tx['tx_date']:%Y%m%d}-{sequence:06d}"
    tx["source_file"] = f"seed/legacy_export_{tx['tx_date']:%Y-%m}.csv"

    history = by_account.setdefault(tx["account_id"], [])
    history.append(tx["tx_date"])
    in_window = sum(1 for moment in history if tx["tx_date"] - VELOCITY_WINDOW < moment <= tx["tx_date"])

    rules = []
    if tx["amount"] > HIGH_AMOUNT_THRESHOLD:
        rules.append(("HIGH_AMOUNT", HIGH_AMOUNT_POINTS,
                      f"Amount {tx['amount']} is above {HIGH_AMOUNT_THRESHOLD}"))
    if in_window > VELOCITY_LIMIT:
        rules.append(("HIGH_VELOCITY", VELOCITY_POINTS,
                      f"{in_window} transactions on the account within 60 minutes"))
    if tx["country_code"] != HOME_COUNTRY:
        rules.append(("CROSS_BORDER", CROSS_BORDER_POINTS,
                      f"Country {tx['country_code']} is not {HOME_COUNTRY}"))

    tx["rules"] = rules
    tx["risk_score"] = min(100, sum(points for _name, points, _reason in rules))
    tx["risk_level"] = (
        "HIGH" if tx["risk_score"] >= HIGH_SCORE
        else "MEDIUM" if tx["risk_score"] >= MEDIUM_SCORE
        else "LOW"
    )


# ---- write SQL ---------------------------------------------------------------
def q(value):
    """Quote a value for SQL."""
    if value is None:
        return "NULL"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return f"'{value.isoformat()}'"
    return "'" + str(value).replace("'", "''") + "'"


def insert(table, columns, rows):
    lines = [f"INSERT INTO {table} ({', '.join(columns)}) VALUES"]
    lines.append(",\n".join("    (" + ", ".join(q(v) for v in row) + ")" for row in rows) + ";")
    return "\n".join(lines) + "\n"


officers = ["t.nkosi", "r.pillay", "m.vanwyk"]
high = [tx for tx in transactions if tx["risk_level"] == "HIGH"]
alert_rows = []
for tx in high:
    created = tx["tx_date"] + timedelta(seconds=4)
    if tx["tx_date"] < at(9, 10, 0, 0):
        alert_rows.append((tx["transaction_id"], "RESOLVED", created,
                           created + timedelta(hours=rng.randint(2, 30)), rng.choice(officers),
                           "Reviewed supporting documents; source of funds confirmed"))
    else:
        alert_rows.append((tx["transaction_id"], "OPEN", created, None, None, None))

event_rows = [
    (tx["account_id"], tx["transaction_id"], name, points, reason, tx["tx_date"] + timedelta(seconds=3))
    for tx in transactions
    for name, points, reason in tx["rules"]
]
event_rows.append(("ACC-1007", None, "MANUAL_FLAG", 0,
                   "Five high-risk cross-border supplier payments within 30 days; "
                   "enhanced due diligence requested",
                   at(9, 16, 9, 12)))

levels = {
    level: sum(1 for tx in transactions if tx["risk_level"] == level)
    for level in ("LOW", "MEDIUM", "HIGH")
}

parts = [
    "-- =============================================================================\n"
    "-- FinTrust Bank SA: seed data (fictional)\n"
    "-- Brief slot: Week 13 Day 2 (Data Layer)\n"
    "--\n"
    "-- GENERATED FILE: do not edit by hand. Regenerate with:\n"
    "--     python scripts/generate_seed.py\n"
    "--\n"
    "-- Run after schema.sql:   psql -f sql/seed.sql\n"
    "--\n"
    f"-- Contents: {len(CUSTOMERS)} customers, {len(ACCOUNTS)} accounts, {len(transactions)} transactions\n"
    f"--           (1 July to 30 September 2026), {len(event_rows)} risk events, {len(alert_rows)} alerts.\n"
    f"-- Risk mix: {levels['LOW']} LOW, {levels['MEDIUM']} MEDIUM, {levels['HIGH']} HIGH.\n"
    "-- Every person, company and account is invented. No real customer data.\n"
    "-- =============================================================================\n\n"
    "BEGIN;\n",
    insert("customers", ["customer_id", "full_name", "email", "phone_number"], CUSTOMERS),
    insert("accounts", ["account_id", "customer_id", "account_type", "status", "opened_date"],
           [row[:5] for row in ACCOUNTS]),
    insert(
        "transactions",
        ["transaction_id", "account_id", "tx_date", "amount", "currency", "tx_type", "channel",
         "merchant_name", "country_code", "description", "risk_score", "risk_level", "source_file"],
        [(tx["transaction_id"], tx["account_id"], tx["tx_date"], tx["amount"], "ZAR", tx["tx_type"],
          tx["channel"], tx["merchant_name"], tx["country_code"], tx["description"], tx["risk_score"],
          tx["risk_level"], tx["source_file"]) for tx in transactions],
    ),
    insert("risk_events",
           ["account_id", "transaction_id", "event_type", "points", "reason", "created_at"], event_rows),
    insert("alerts", ["transaction_id", "status", "created_at", "resolved_at", "resolved_by",
                      "resolution_note"], alert_rows),
    "COMMIT;\n",
]

OUTPUT.write_text("\n".join(parts))
print(f"Wrote {OUTPUT.relative_to(OUTPUT.parents[1])}: {len(transactions)} transactions, "
      f"{levels}, {len(event_rows)} risk events, {len(alert_rows)} alerts")
