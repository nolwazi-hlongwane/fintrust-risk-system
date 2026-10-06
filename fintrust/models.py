"""Data models: the shapes that move between the modules.

    Transaction   one validated row from a CSV file
    RuleHit       one risk rule that fired, with the points it added and why
    RiskScore     the score for one transaction, with every RuleHit (this is
                  what makes a score explainable)

They are frozen dataclasses: once created, a transaction cannot be changed by
accident somewhere else in the code.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")


def to_utc_text(moment):
    """Format a datetime as UTC text, e.g. 2026-09-15T07:42:11Z.

    Used for the DynamoDB sort key. Text in this format sorts in time order,
    which is what makes "newest first for this account" a cheap query.
    """
    if moment.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_datetime(text):
    """Parse ISO text (with a timezone) into a datetime."""
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"datetime '{text}' has no timezone")
    return moment


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    account_id: str
    tx_date: datetime
    amount: Decimal
    tx_type: str
    channel: str
    country_code: str
    source_file: str
    currency: str = "ZAR"
    merchant_name: str | None = None
    description: str | None = None

    def __post_init__(self):
        if not isinstance(self.amount, Decimal):
            raise TypeError("amount must be a Decimal, never a float")
        if self.tx_date.tzinfo is None:
            raise ValueError("tx_date must include a timezone")

    @property
    def tx_timestamp(self):
        """The transaction time as UTC text, used in the DynamoDB sort key."""
        return to_utc_text(self.tx_date)

    def to_message(self):
        """Plain dict that can be sent as JSON in an SQS message."""
        return {
            "transaction_id": self.transaction_id,
            "account_id": self.account_id,
            "tx_date": self.tx_date.isoformat(),
            "amount": str(self.amount),
            "currency": self.currency,
            "tx_type": self.tx_type,
            "channel": self.channel,
            "merchant_name": self.merchant_name,
            "country_code": self.country_code,
            "description": self.description,
            "source_file": self.source_file,
        }

    @classmethod
    def from_message(cls, message):
        """Rebuild a Transaction from the dict produced by to_message()."""
        return cls(
            transaction_id=message["transaction_id"],
            account_id=message["account_id"],
            tx_date=parse_datetime(message["tx_date"]),
            amount=Decimal(message["amount"]),
            currency=message.get("currency", "ZAR"),
            tx_type=message["tx_type"],
            channel=message["channel"],
            merchant_name=message.get("merchant_name"),
            country_code=message["country_code"],
            description=message.get("description"),
            source_file=message["source_file"],
        )


@dataclass(frozen=True)
class RuleHit:
    rule: str      # HIGH_AMOUNT, HIGH_VELOCITY or CROSS_BORDER
    points: int
    detail: str    # plain-language reason, shown on the dashboard and in the email

    def to_dict(self):
        return {"rule": self.rule, "points": self.points, "detail": self.detail}


@dataclass(frozen=True)
class RiskScore:
    transaction_id: str
    account_id: str
    tx_timestamp: str          # UTC text, from Transaction.tx_timestamp
    risk_score: int
    risk_level: str
    rules: tuple = field(default_factory=tuple)   # tuple of RuleHit
    scored_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"risk_level must be one of {RISK_LEVELS}")
        if not 0 <= self.risk_score <= 100:
            raise ValueError("risk_score must be between 0 and 100")

    @property
    def is_high_risk(self):
        return self.risk_level == "HIGH"

    def to_dict(self):
        return {
            "transaction_id": self.transaction_id,
            "account_id": self.account_id,
            "tx_timestamp": self.tx_timestamp,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "rules": [hit.to_dict() for hit in self.rules],
            "scored_at": to_utc_text(self.scored_at),
        }
