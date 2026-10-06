"""Run SQL files or a single query against the FinTrust database as the admin user.

The tunnel must be open in another terminal window first: ./scripts/db_tunnel.sh

    python scripts/run_sql.py sql/schema.sql sql/db_users.sql sql/seed.sql
    python scripts/run_sql.py -c "SELECT risk_level, count(*) FROM transactions GROUP BY 1"

The admin password is read from SSM Parameter Store (encrypted) at run time. It
is never typed, printed or stored on this computer.

To run against a local PostgreSQL instead (docker compose), set RUN_SQL_LOCAL=1:

    RUN_SQL_LOCAL=1 python scripts/run_sql.py sql/schema.sql
"""

import argparse
import os
import sys
from pathlib import Path

import boto3
import psycopg2

REGION = os.environ.get("AWS_REGION", "eu-north-1")
ENVIRONMENT = os.environ.get("FINTRUST_ENV", "dev")
MASTER_PASSWORD_PARAMETER = f"/fintrust-admin/{ENVIRONMENT}/db_master_password"


def connection_settings():
    if os.environ.get("RUN_SQL_LOCAL") == "1":
        return {
            "host": os.environ.get("PGHOST", "localhost"),
            "port": os.environ.get("PGPORT", "5433"),
            "dbname": os.environ.get("PGDATABASE", "fintrust"),
            "user": os.environ.get("PGUSER", "postgres"),
            "password": os.environ.get("PGPASSWORD", "local-test-only"),
        }
    ssm = boto3.client("ssm", region_name=REGION)
    password = ssm.get_parameter(Name=MASTER_PASSWORD_PARAMETER, WithDecryption=True)["Parameter"]["Value"]
    return {
        "host": "localhost",       # the local end of the tunnel
        "port": "15432",
        "dbname": "fintrust",
        "user": "fintrust_admin",
        "password": password,
        "sslmode": "require",      # encrypted all the way to RDS
    }


def print_rows(cursor):
    """Print a result set as a simple aligned table."""
    headers = [column.name for column in cursor.description]
    rows = [["" if value is None else str(value) for value in row] for row in cursor.fetchall()]
    widths = [max(len(header), *(len(row[i]) for row in rows)) if rows else len(header)
              for i, header in enumerate(headers)]
    print(" | ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)))
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


def run(cursor, label, sql):
    print(f"\n== {label}")
    cursor.execute(sql)
    if cursor.description:
        print_rows(cursor)
    else:
        print("OK")


def main():
    parser = argparse.ArgumentParser(description="Run SQL against the FinTrust database.")
    parser.add_argument("files", nargs="*", type=Path, help="SQL files, run in the order given")
    parser.add_argument("-c", "--command", help="a single SQL statement to run")
    arguments = parser.parse_args()
    if not arguments.files and not arguments.command:
        parser.error("give at least one SQL file, or -c \"SQL\"")
    for path in arguments.files:
        if not path.is_file():
            parser.error(f"file not found: {path}")

    try:
        connection = psycopg2.connect(connect_timeout=10, **connection_settings())
    except psycopg2.OperationalError as error:
        print(f"Could not connect to the database:\n  {str(error).strip()}", file=sys.stderr)
        print("Is the tunnel open in another window?  ./scripts/db_tunnel.sh", file=sys.stderr)
        return 1

    # The SQL files contain their own BEGIN and COMMIT, so each file is applied
    # completely or not at all.
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            for path in arguments.files:
                run(cursor, path, path.read_text())
            if arguments.command:
                run(cursor, "query", arguments.command)
    except psycopg2.Error as error:
        try:
            connection.rollback()
        except psycopg2.Error:
            pass
        print(f"\nFAILED: {str(error).strip()}", file=sys.stderr)
        print("Nothing from the failed file was applied.", file=sys.stderr)
        return 1
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
