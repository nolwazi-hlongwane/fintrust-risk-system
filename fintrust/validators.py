"""Input sanitisation and POPIA-aware validation of transaction rows.

Every CSV row passes through validate_row() before anything is stored or
queued. A row either becomes a clean Transaction, or it is rejected with EVERY
reason listed (not just the first), so the data steward can fix it in one go.

Data quality dimensions checked:
    completeness   required fields are present
    validity       formats, allowed values, positive exact amounts
    timeliness     the transaction date is not in the future
    uniqueness     checked by the caller across the whole file
    consistency    identifiers match the bank's formats

POPIA (minimality, section 10): the pipeline needs account ids, not people.
    * Columns the system does not need are never read (see IGNORED BY DESIGN).
    * Free-text fields are scanned for personal identifiers that should not be
      there (email addresses, phone numbers, ID or card numbers). These are
      replaced with [REDACTED] and a warning is recorded, so they never reach
      the ledger, the queue or the logs.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fintrust.models import Transaction

REQUIRED_COLUMNS = ("transaction_id", "account_id", "tx_date", "amount", "tx_type", "channel", "country_code")
OPTIONAL_COLUMNS = ("currency", "merchant_name", "description")
COLUMN_ALIASES = {"merchant": "merchant_name"}   # accepted spelling -> canonical name

TX_TYPES = ("CREDIT", "DEBIT")
CHANNELS = ("POS", "ONLINE", "ATM", "EFT", "MOBILE", "BRANCH")

MAX_AMOUNT = Decimal("999999999999.99")          # fits NUMERIC(14,2) in the ledger
MAX_MERCHANT_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 255
CLOCK_SKEW = timedelta(minutes=5)                # tolerance for "not in the future"
EARLIEST_DATE = datetime(2000, 1, 1, tzinfo=timezone.utc)

# Legacy exports carry local time with no offset. FinTrust operates in South
# Africa, which is UTC+02:00 all year (no daylight saving).
DEFAULT_TIMEZONE = timezone(timedelta(hours=2))

TRANSACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{5,39}$")
ACCOUNT_ID_PATTERN = re.compile(r"^ACC-[0-9]{4,}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
COUNTRY_PATTERN = re.compile(r"^[A-Z]{2}$")
AMOUNT_PATTERN = re.compile(r"^[0-9]+(\.[0-9]{1,2})?$")

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 13 to 19 digits, optionally grouped with spaces or dashes: covers South
# African ID numbers (13 digits) and payment card numbers (13 to 19 digits).
LONG_NUMBER_PATTERN = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])")
PHONE_PATTERN = re.compile(r"(?<![0-9])(?:\+27|0)[ -]?[1-8][0-9][ -]?[0-9]{3}[ -]?[0-9]{4}(?![0-9])")

REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class RowResult:
    transaction: Transaction | None   # None when the row was rejected
    errors: tuple = ()                # why it was rejected
    warnings: tuple = ()              # accepted, but something was cleaned

    @property
    def is_valid(self):
        return self.transaction is not None


def mask_account(account_id):
    """ACC-1007 -> ACC-**07. Used wherever an account appears in an email or log."""
    text = str(account_id or "")
    if len(text) <= 2:
        return "**"
    prefix, _separator, number = text.rpartition("-")
    if not prefix:
        return "*" * (len(text) - 2) + text[-2:]
    return f"{prefix}-{'*' * max(len(number) - 2, 0)}{number[-2:]}"


def normalise_header(fieldnames):
    """Lower-case, trim and apply aliases to the CSV header names."""
    names = []
    for name in fieldnames or []:
        clean = (name or "").strip().lower().lstrip("﻿")
        names.append(COLUMN_ALIASES.get(clean, clean))
    return names


def validate_header(fieldnames):
    """Return a list of problems with the header row (empty list = fine)."""
    names = normalise_header(fieldnames)
    problems = []
    missing = [column for column in REQUIRED_COLUMNS if column not in names]
    if missing:
        problems.append(f"missing required column(s): {', '.join(missing)}")
    duplicates = sorted({name for name in names if name and names.count(name) > 1})
    if duplicates:
        problems.append(f"duplicate column(s): {', '.join(duplicates)}")
    return problems


def ignored_columns(fieldnames):
    """Columns present in the file that the system does not need and never reads."""
    known = set(REQUIRED_COLUMNS) | set(OPTIONAL_COLUMNS)
    return [name for name in normalise_header(fieldnames) if name and name not in known]


def redact_personal_information(text):
    """Replace personal identifiers in free text. Returns (clean_text, kinds_found)."""
    found = []
    for kind, pattern in (
        ("email address", EMAIL_PATTERN),
        ("phone number", PHONE_PATTERN),          # before long numbers, which would swallow it
        ("ID or card number", LONG_NUMBER_PATTERN),
    ):
        text, count = pattern.subn(REDACTED, text)
        if count:
            found.append(kind)
    return text, found


def sanitise_text(value, field, max_length):
    """Clean one free-text field. Returns (text or None, warnings)."""
    if value is None:
        return None, []
    warnings = []
    # Remove control characters (tabs, newlines, null bytes, ...) and collapse spaces.
    text = "".join(" " if unicodedata.category(char).startswith("C") else char for char in str(value))
    text = " ".join(text.split())
    if not text:
        return None, []

    text, found = redact_personal_information(text)
    for kind in found:
        warnings.append(f"{field} contained a possible {kind}, which was redacted")

    # A value starting with = + - or @ is run as a formula if the file is later
    # opened in a spreadsheet ("CSV injection"). A leading apostrophe makes it text.
    if text[0] in "=+-@":
        text = "'" + text
        warnings.append(f"{field} started with a formula character and was neutralised")

    if len(text) > max_length:
        text = text[:max_length]
        warnings.append(f"{field} was longer than {max_length} characters and was shortened")
    return text, warnings


def parse_tx_date(text):
    """Parse a transaction date. Raises ValueError with a readable message."""
    candidate = text.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValueError(
            f"tx_date '{text}' is not a valid date and time (expected e.g. 2026-10-01T09:30:00+02:00)"
        ) from None
    if "T" not in candidate.upper() and " " not in candidate:
        raise ValueError(f"tx_date '{text}' has no time of day")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=DEFAULT_TIMEZONE)
    return moment


def parse_amount(text):
    """Parse an amount into an exact Decimal. Raises ValueError with a readable message."""
    candidate = text.strip()
    if not AMOUNT_PATTERN.match(candidate):
        raise ValueError(
            f"amount '{text}' must be a positive number with at most 2 decimals "
            "and no currency symbol or thousands separator"
        )
    try:
        amount = Decimal(candidate)
    except InvalidOperation:
        raise ValueError(f"amount '{text}' is not a number") from None
    if amount <= 0:
        raise ValueError("amount must be greater than zero")
    if amount > MAX_AMOUNT:
        raise ValueError(f"amount {amount} is larger than the ledger can store")
    return amount.quantize(Decimal("0.01"))


def validate_row(row, source_file, now=None):
    """Validate and clean one CSV row (a dict keyed by normalised column names).

    Returns a RowResult. `now` can be passed in so tests are repeatable.
    """
    now = now or datetime.now(timezone.utc)
    errors = []
    warnings = []

    def field(name):
        value = row.get(name)
        return value.strip() if isinstance(value, str) else ""

    for name in REQUIRED_COLUMNS:
        if not field(name):
            errors.append(f"{name} is required")

    transaction_id = field("transaction_id")
    if transaction_id and not TRANSACTION_ID_PATTERN.match(transaction_id):
        errors.append("transaction_id must be 6 to 40 letters, digits or dashes")

    account_id = field("account_id").upper()
    if account_id and not ACCOUNT_ID_PATTERN.match(account_id):
        errors.append("account_id must look like ACC-1234")

    tx_date = None
    if field("tx_date"):
        try:
            tx_date = parse_tx_date(field("tx_date"))
        except ValueError as error:
            errors.append(str(error))
        else:
            if tx_date > now + CLOCK_SKEW:
                errors.append("tx_date is in the future")
            elif tx_date < EARLIEST_DATE:
                errors.append("tx_date is before the year 2000")

    amount = None
    if field("amount"):
        try:
            amount = parse_amount(field("amount"))
        except ValueError as error:
            errors.append(str(error))

    tx_type = field("tx_type").upper()
    if tx_type and tx_type not in TX_TYPES:
        errors.append(f"tx_type must be one of {', '.join(TX_TYPES)}")

    channel = field("channel").upper()
    if channel and channel not in CHANNELS:
        errors.append(f"channel must be one of {', '.join(CHANNELS)}")

    country_code = field("country_code").upper()
    if country_code and not COUNTRY_PATTERN.match(country_code):
        errors.append("country_code must be a 2-letter code such as ZA")

    currency = field("currency").upper() or "ZAR"
    if not CURRENCY_PATTERN.match(currency):
        errors.append("currency must be a 3-letter code such as ZAR")

    merchant_name, merchant_warnings = sanitise_text(row.get("merchant_name"), "merchant_name", MAX_MERCHANT_LENGTH)
    description, description_warnings = sanitise_text(row.get("description"), "description", MAX_DESCRIPTION_LENGTH)
    warnings.extend(merchant_warnings)
    warnings.extend(description_warnings)

    if errors:
        return RowResult(None, tuple(errors), tuple(warnings))

    transaction = Transaction(
        transaction_id=transaction_id,
        account_id=account_id,
        tx_date=tx_date,
        amount=amount,
        currency=currency,
        tx_type=tx_type,
        channel=channel,
        merchant_name=merchant_name,
        country_code=country_code,
        description=description,
        source_file=source_file,
    )
    return RowResult(transaction, (), tuple(warnings))
