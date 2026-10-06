"""Risk rules: the single place where the scoring logic lives.

Pure functions only. Nothing here touches AWS or the database, so the rules can
be unit tested on their own and used by two Lambdas:

    ingest  runs the rules that need only the row itself (the "pre-score")
    score   runs every rule, including velocity, which needs the account's
            recent history from the ledger

Every number comes from configuration (fintrust/config.py), so a threshold can
be changed in SSM Parameter Store without changing code.

    HIGH_AMOUNT    amount is above the threshold (default R50 000)       +50
    HIGH_VELOCITY  more than 10 transactions on the account in 60 min    +30
    CROSS_BORDER   country is not the home country (default ZA)          +20

    score 0 to 39 = LOW,  40 to 69 = MEDIUM,  70 to 100 = HIGH (sends an alert)
"""

from fintrust import config
from fintrust.models import RiskScore, RuleHit


def high_amount_hit(transaction):
    threshold = config.get_decimal("high_amount_threshold")
    if transaction.amount > threshold:
        return RuleHit(
            "HIGH_AMOUNT",
            config.get_int("high_amount_points"),
            f"Amount {transaction.amount} is above {threshold}",
        )
    return None


def cross_border_hit(transaction):
    home = config.get("home_country")
    if transaction.country_code != home:
        return RuleHit(
            "CROSS_BORDER",
            config.get_int("cross_border_points"),
            f"Country {transaction.country_code} is not {home}",
        )
    return None


def velocity_hit(transactions_in_window):
    """`transactions_in_window` counts this transaction plus the account's
    other transactions in the window before it."""
    if transactions_in_window > config.get_int("velocity_limit"):
        minutes = config.get_int("velocity_window_minutes")
        return RuleHit(
            "HIGH_VELOCITY",
            config.get_int("velocity_points"),
            f"{transactions_in_window} transactions on the account within {minutes} minutes",
        )
    return None


def stateless_hits(transaction):
    """Rules that need only the transaction itself. Used for the pre-score."""
    hits = (high_amount_hit(transaction), cross_border_hit(transaction))
    return tuple(hit for hit in hits if hit is not None)


def all_hits(transaction, transactions_in_window):
    """Every rule, in a fixed order."""
    hits = (
        high_amount_hit(transaction),
        velocity_hit(transactions_in_window),
        cross_border_hit(transaction),
    )
    return tuple(hit for hit in hits if hit is not None)


def total_points(hits):
    return min(100, sum(hit.points for hit in hits))


def classify(score):
    if score >= config.get_int("high_risk_score"):
        return "HIGH"
    if score >= config.get_int("medium_risk_score"):
        return "MEDIUM"
    return "LOW"


def build_score(transaction, hits):
    """Turn a transaction and the rules that fired into a RiskScore."""
    score = total_points(hits)
    return RiskScore(
        transaction_id=transaction.transaction_id,
        account_id=transaction.account_id,
        tx_timestamp=transaction.tx_timestamp,
        risk_score=score,
        risk_level=classify(score),
        rules=tuple(hits),
    )
