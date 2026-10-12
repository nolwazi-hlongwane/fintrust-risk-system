"""Write a fresh CSV file for a live demonstration.

    python scripts/make_demo_file.py

Every run produces new transaction ids and a new "not yet onboarded" account,
so the demonstration can be repeated as often as needed (rehearsals included)
and always behaves the same way.

The file holds 12 rows that between them show the whole system:

    7  ordinary transactions            scored LOW
    1  large local payment              MEDIUM (high amount)
    1  large cross-border payment       HIGH: alert email
    2  invalid rows                     set aside in the quarantine bucket, never stored
    1  transaction for an account that is not in the ledger yet
                                        fails 3 times, lands in the dead-letter
                                        queue, and is recovered by onboarding
                                        the account and redriving the queue

The file is written to build/demo/ (not committed). The steps to follow are
printed, and are also in docs/demo-runbook.md.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SAST = timezone(timedelta(hours=2))
OUT_DIR = Path(__file__).resolve().parents[1] / "build" / "demo"
HEADER = "transaction_id,account_id,tx_date,amount,currency,tx_type,channel,merchant_name,country_code,description"


def build(now=None):
    """Return (file name, CSV text, id of the HIGH row, id of the dead-letter row, the new account id)."""
    now = (now or datetime.now(SAST)).astimezone(SAST)
    stamp = now.strftime("%m%d%H%M%S")
    new_account = f"ACC-9{stamp}"
    start = now - timedelta(hours=3)            # every row is safely in the past

    def row(number, account, minutes, amount, tx_type, channel, merchant, country, description):
        when = (start + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        transaction_id = f"TXN-{now:%Y%m%d}-D{stamp[4:]}{number:02d}"
        fields = [transaction_id, account, when, amount, "ZAR", tx_type, channel, merchant, country, description]
        return transaction_id, ",".join(fields)

    rows = [
        row(1, "ACC-1001", 0, "412.50", "DEBIT", "POS", "Checkers Sandton", "ZA", "Groceries"),
        row(2, "ACC-1004", 6, "899.00", "DEBIT", "ONLINE", "Takealot", "ZA", "Online shopping"),
        row(3, "ACC-1006", 11, "640.75", "DEBIT", "POS", "Engen Garage", "ZA", "Fuel"),
        row(4, "ACC-1010", 17, "75000.00", "DEBIT", "EFT", "Sanlam Investments", "ZA", "Investment contribution"),
        row(5, "ACC-1007", 24, "88750.00", "DEBIT", "EFT", "Dubai Marine Supplies LLC", "AE", "Supplier payment"),
        row(6, "ACC-1008", 31, "-250.00", "DEBIT", "ATM", "", "ZA", "Negative amount"),
        row(7, "1009", 38, "120.00", "DEBIT", "POS", "Spar", "ZA", "Account number in the wrong format"),
        row(8, new_account, 44, "1850.00", "DEBIT", "ONLINE", "Mr Price", "ZA", "Account not onboarded yet"),
        row(9, "ACC-1003", 52, "150.00", "DEBIT", "MOBILE", "MTN Airtime", "ZA", "Airtime and data"),
        row(10, "ACC-1001", 58, "23400.00", "CREDIT", "EFT", "Employer payroll", "ZA", "Salary"),
        row(11, "ACC-1004", 66, "2350.00", "DEBIT", "ONLINE", "Booking.com", "NL", "International purchase"),
        row(12, "ACC-1008", 73, "95.00", "DEBIT", "POS", "Nando's", "ZA", "Restaurant"),
    ]
    text = "\n".join([HEADER, *(line for _id, line in rows)]) + "\n"
    return f"fintrust-demo-{stamp}.csv", text, rows[4][0], rows[7][0], new_account


def main():
    name, text, high_id, dead_letter_id, new_account = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    path.write_text(text, encoding="utf-8")
    print(f"Written: {path}\n")
    print("What to expect after uploading it on the dashboard (Upload a file):")
    print("  9 rows scored within about 20 seconds; 2 rows show \"No record yet\" (invalid, quarantined)")
    print(f"  {high_id}  HIGH, 70 points: alert email arrives")
    print(f"  {dead_letter_id}  Retrying, then \"In the dead-letter queue\" after about 3 minutes")
    print("\nTo recover it (database tunnel open, at least 5 minutes after the upload):")
    print(f"  python scripts/onboard_account.py {new_account}")
    print("  ./scripts/redrive_dlq.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
