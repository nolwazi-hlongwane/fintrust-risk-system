"""Unit tests for fintrust.models."""

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fintrust.models import RiskScore, RuleHit, Transaction, parse_datetime, to_utc_text

SAST = timezone(timedelta(hours=2))


def make_transaction(**overrides):
    values = {
        "transaction_id": "TXN-20260915-000231",
        "account_id": "ACC-1007",
        "tx_date": datetime(2026, 9, 15, 15, 10, 0, tzinfo=SAST),
        "amount": Decimal("104500.00"),
        "tx_type": "DEBIT",
        "channel": "EFT",
        "country_code": "AE",
        "source_file": "uploads/2026-09-15/batch.csv",
        "merchant_name": "Dubai Marine Supplies LLC",
        "description": "Supplier payment",
    }
    values.update(overrides)
    return Transaction(**values)


class TestTransaction:
    def test_timestamp_is_converted_to_utc(self):
        assert make_transaction().tx_timestamp == "2026-09-15T13:10:00Z"

    def test_float_amount_is_rejected(self):
        with pytest.raises(TypeError, match="Decimal"):
            make_transaction(amount=104500.00)

    def test_date_without_timezone_is_rejected(self):
        with pytest.raises(ValueError, match="timezone"):
            make_transaction(tx_date=datetime(2026, 9, 15, 15, 10))

    def test_cannot_be_changed_after_creation(self):
        with pytest.raises(FrozenInstanceError):
            make_transaction().amount = Decimal("1.00")

    def test_message_is_json_serialisable(self):
        text = json.dumps(make_transaction().to_message())
        assert '"amount": "104500.00"' in text

    def test_message_round_trip_loses_nothing(self):
        original = make_transaction()
        rebuilt = Transaction.from_message(json.loads(json.dumps(original.to_message())))
        assert rebuilt == original
        assert rebuilt.amount == Decimal("104500.00")

    def test_round_trip_with_optional_fields_missing(self):
        original = make_transaction(merchant_name=None, description=None)
        assert Transaction.from_message(original.to_message()) == original

    def test_currency_defaults_to_rand(self):
        message = make_transaction().to_message()
        del message["currency"]
        assert Transaction.from_message(message).currency == "ZAR"

    def test_message_missing_required_field_raises(self):
        message = make_transaction().to_message()
        del message["account_id"]
        with pytest.raises(KeyError):
            Transaction.from_message(message)


class TestRiskScore:
    def make(self, **overrides):
        values = {
            "transaction_id": "TXN-20260915-000231",
            "account_id": "ACC-1007",
            "tx_timestamp": "2026-09-15T13:10:00Z",
            "risk_score": 70,
            "risk_level": "HIGH",
            "rules": (
                RuleHit("HIGH_AMOUNT", 50, "Amount 104500.00 is above 50000"),
                RuleHit("CROSS_BORDER", 20, "Country AE is not ZA"),
            ),
            "scored_at": datetime(2026, 9, 15, 13, 10, 4, tzinfo=timezone.utc),
        }
        values.update(overrides)
        return RiskScore(**values)

    def test_high_risk_flag(self):
        assert self.make().is_high_risk is True
        assert self.make(risk_score=20, risk_level="LOW").is_high_risk is False

    def test_unknown_level_is_rejected(self):
        with pytest.raises(ValueError, match="risk_level"):
            self.make(risk_level="CRITICAL")

    @pytest.mark.parametrize("score", [-1, 101])
    def test_score_out_of_range_is_rejected(self, score):
        with pytest.raises(ValueError, match="between 0 and 100"):
            self.make(risk_score=score)

    def test_dict_carries_the_rule_breakdown(self):
        data = self.make().to_dict()
        assert data["rules"] == [
            {"rule": "HIGH_AMOUNT", "points": 50, "detail": "Amount 104500.00 is above 50000"},
            {"rule": "CROSS_BORDER", "points": 20, "detail": "Country AE is not ZA"},
        ]
        assert data["scored_at"] == "2026-09-15T13:10:04Z"
        json.dumps(data)  # must not raise

    def test_scored_at_defaults_to_now(self):
        score = RiskScore("T", "A", "2026-09-15T13:10:00Z", 0, "LOW")
        assert score.scored_at.tzinfo is not None
        assert score.rules == ()


class TestDateHelpers:
    def test_utc_text_sorts_in_time_order(self):
        earlier = to_utc_text(datetime(2026, 9, 15, 23, 59, tzinfo=SAST))
        later = to_utc_text(datetime(2026, 9, 15, 22, 0, tzinfo=timezone.utc))
        assert earlier < later

    def test_to_utc_text_needs_a_timezone(self):
        with pytest.raises(ValueError):
            to_utc_text(datetime(2026, 9, 15, 12, 0))

    def test_parse_accepts_z_suffix(self):
        assert parse_datetime("2026-09-15T13:10:00Z") == datetime(2026, 9, 15, 13, 10, tzinfo=timezone.utc)

    def test_parse_rejects_missing_timezone(self):
        with pytest.raises(ValueError, match="no timezone"):
            parse_datetime("2026-09-15T13:10:00")
