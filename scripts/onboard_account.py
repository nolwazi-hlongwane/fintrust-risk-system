"""Onboard a new customer account in the ledger (through the private tunnel).

    python scripts/onboard_account.py ACC-9001

This is the "fix the cause" step of the dead-letter queue recovery: a
transaction for an account that is not in the ledger cannot be scored, so its
message waits in the dead-letter queue. Once the account exists, run
./scripts/redrive_dlq.sh and the waiting transaction is processed.

The tunnel must be open in another terminal window: ./scripts/db_tunnel.sh

In a real bank the account would come from the core banking system's
onboarding process. Here one customer and one cheque account are inserted,
in a single database transaction. Running it again for the same account changes
nothing.
"""

import re
import sys

import psycopg2
from run_sql import connection_settings

ACCOUNT_PATTERN = re.compile(r"^ACC-([0-9]{4,})$")

# Fictional customers, like the seed data: the same account number always gets
# the same person, so a rehearsal and the live demonstration look identical.
# Contact details use the reserved example.com domain and the 555 range, so no
# real person can ever be emailed or called (POPIA).
FICTIONAL_CUSTOMERS = (
    ("Naledi", "Mthembu"), ("Ruan", "Pretorius"), ("Fatima", "Essop"), ("Kagiso", "Molefe"),
    ("Anele", "Ndlovu"), ("Charlene", "Jacobs"), ("Bongani", "Sithole"), ("Priya", "Govender"),
)


def customer_details(number):
    """Name, email and phone number for the customer behind account ACC-<number>."""
    first, last = FICTIONAL_CUSTOMERS[int(number) % len(FICTIONAL_CUSTOMERS)]
    return f"{first} {last}", f"{first}.{last}.{number}@example.com".lower(), f"+27 82 555 {number[-4:]}"


def onboard(account_id, connection=None):
    """Insert the customer and account if missing. Returns True if the account was created."""
    match = ACCOUNT_PATTERN.match(account_id)
    if not match:
        raise ValueError("account id must look like ACC-9001")
    number = match.group(1)
    customer_id = f"CUST-{number}"

    own_connection = connection is None
    if own_connection:
        connection = psycopg2.connect(connect_timeout=10, **connection_settings())
    try:
        with connection:                      # commits on success, rolls back on any error
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM accounts WHERE account_id = %s", (account_id,))
                if cursor.fetchone():
                    return False              # already there: change nothing, not even a customer row
                cursor.execute(
                    "INSERT INTO customers (customer_id, full_name, email, phone_number) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (customer_id) DO NOTHING",
                    (customer_id, *customer_details(number)),
                )
                cursor.execute(
                    "INSERT INTO accounts (account_id, customer_id, account_type, opened_date) "
                    "VALUES (%s, %s, 'CHEQUE', CURRENT_DATE) ON CONFLICT (account_id) DO NOTHING",
                    (account_id, customer_id),
                )
                return cursor.rowcount == 1
    finally:
        if own_connection:
            connection.close()


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/onboard_account.py ACC-9001", file=sys.stderr)
        return 1
    try:
        created = onboard(sys.argv[1].strip().upper())
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except psycopg2.OperationalError as error:
        print(f"Could not connect to the database:\n  {str(error).strip()}", file=sys.stderr)
        print("Is the tunnel open in another window?  ./scripts/db_tunnel.sh", file=sys.stderr)
        return 1
    except psycopg2.Error as error:
        print(f"FAILED: {str(error).strip()}\nNothing was changed.", file=sys.stderr)
        return 1
    print(f"Account {sys.argv[1].strip().upper()} " + ("onboarded." if created else "already exists; nothing changed."))
    print("Next: ./scripts/redrive_dlq.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
