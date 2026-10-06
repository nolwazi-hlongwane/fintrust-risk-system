"""PostgreSQL access: one reused connection, and rollback on every error.

The two patterns this module exists to enforce:

1. CONNECTION REUSE. Opening a database connection is slow (network, TLS,
   login). The connection is kept in a module-level variable, so a warm Lambda
   container reuses it for every invocation instead of opening a new one each
   time. One Lambda container handles one request at a time, so one connection
   per container is exactly enough. It also protects the small db.t3.micro
   instance from running out of connections.

2. ROLLBACK ON EVERY ERROR. Work is done inside `with transaction() as cursor`.
   If the block finishes, the changes are committed together. If ANY exception
   is raised, conn.rollback() is called before the error is passed on, so a
   half-finished write can never stay in the ledger and the reused connection
   is never left stuck in a failed state.

How the Lambda logs in (db_auth_mode):
   "iam"       In AWS. No password exists. The Lambda's IAM role signs a login
               token that is valid for 15 minutes. Signing happens locally, with
               no network call, which is why it works inside the VPC without a
               NAT Gateway. RDS only accepts these tokens over TLS.
   "password"  Local development and CI tests against a PostgreSQL container.
"""

from contextlib import contextmanager

import boto3
import psycopg2
from psycopg2.extras import RealDictCursor

from fintrust import config
from fintrust.log import get_logger

logger = get_logger("db")

# Module level: created once per Lambda container. Used only to SIGN the login
# token; it never calls the RDS API.
_rds = boto3.client("rds", region_name=config.region())

# The one reused connection for this container.
_connection = None


class DatabaseError(Exception):
    """A database operation failed. The transaction has been rolled back."""


class DatabaseUnavailable(DatabaseError):
    """The database could not be reached at all (for example, RDS is stopped)."""


def _connect():
    """Open a new connection using settings from config."""
    host = config.get("db_host")
    port = config.get_int("db_port")
    user = config.get("db_user")
    mode = config.get("db_auth_mode")

    if mode == "iam":
        password = _rds.generate_db_auth_token(
            DBHostname=host, Port=port, DBUsername=user, Region=config.region()
        )
        sslmode = "require"
    elif mode == "password":
        password = config.get("db_password")
        sslmode = config.get("db_sslmode", "prefer")
    else:
        raise config.ConfigError(f"db_auth_mode must be 'iam' or 'password', got '{mode}'")

    connection = psycopg2.connect(
        host=host,
        port=port,
        dbname=config.get("db_name"),
        user=user,
        password=password,
        sslmode=sslmode,
        connect_timeout=5,
        # Stop any single statement after 10 seconds instead of hanging until
        # the Lambda itself times out.
        options="-c statement_timeout=10000",
        application_name="fintrust",
    )
    logger.info("db_connected", db_user=user, auth_mode=mode)
    return connection


def get_connection():
    """Return the reused connection, opening one only if needed."""
    global _connection
    if _connection is None or _connection.closed:
        try:
            _connection = _connect()
        except psycopg2.Error as error:
            _connection = None
            logger.error("db_connect_failed", error=str(error).strip())
            raise DatabaseUnavailable("Could not connect to the database") from error
    return _connection


def close_connection():
    """Close and forget the connection. The next call reconnects."""
    global _connection
    if _connection is not None:
        try:
            _connection.close()
        except psycopg2.Error:
            pass
    _connection = None


def _rollback(connection):
    """Roll back; if the connection itself is broken, discard it."""
    try:
        connection.rollback()
    except psycopg2.Error:
        close_connection()
        return
    if connection.closed:
        close_connection()


@contextmanager
def transaction():
    """Run several statements as one all-or-nothing unit.

        with db.transaction() as cursor:
            cursor.execute("INSERT ...", params)
            cursor.execute("INSERT ...", params)

    Commits when the block ends normally. Rolls back on any exception.
    Rows come back as dictionaries, e.g. row["account_id"].
    """
    connection = get_connection()
    cursor = None
    try:
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        yield cursor
        connection.commit()
    except psycopg2.Error as error:
        _rollback(connection)
        if isinstance(error, (psycopg2.OperationalError, psycopg2.InterfaceError)):
            # The connection is dead (for example RDS was restarted). Drop it so
            # the next invocation opens a fresh one.
            close_connection()
        logger.error(
            "db_transaction_rolled_back",
            error_type=type(error).__name__,
            sqlstate=getattr(error, "pgcode", None),
            error=str(error).strip(),
        )
        raise DatabaseError(str(error).strip()) from error
    except Exception:
        # Not a database error (for example a bug in the calling code), but the
        # work done so far must still be undone.
        _rollback(connection)
        raise
    finally:
        if cursor is not None and not cursor.closed:
            try:
                cursor.close()
            except psycopg2.Error:
                pass


def fetch_all(sql, params=None):
    """Run one SELECT and return every row as a list of dicts."""
    with transaction() as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def fetch_one(sql, params=None):
    """Run one SELECT and return the first row as a dict, or None."""
    with transaction() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
        return dict(row) if row is not None else None


def execute(sql, params=None):
    """Run one INSERT, UPDATE or DELETE and return how many rows it changed."""
    with transaction() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def is_healthy():
    """True if the database answers a trivial query. Used by GET /health."""
    try:
        return fetch_one("SELECT 1 AS ok") == {"ok": 1}
    except DatabaseError:
        return False
