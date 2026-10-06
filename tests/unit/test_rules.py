"""Unit tests for fintrust.rules: the risk scoring rules."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fintrust import rules
from fintrust.models import Transaction

SAST = timezone(timedelta(hours=2))


@pytest.fixture(autouse=True)
def no_ssm(monkeypatch):
    monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")


def tx(amount="100.00", country="ZA"):
    return Transaction(
        transaction_id="TXN-20261001-900001",
        account_id="ACC-1001",
        tx_date=datetime(2026, 10, 1, 9, 30, tzinfo=SAST),
        amount=Decimal(amount),
        tx_type="DEBIT",
        channel="EFT",
        country_code=country,
        source_file="test.csv",
    )


class TestHighAmount:
    def test_exactly_at_threshold_does_not_fire(self):
        assert rules.high_amount_hit(tx("50000.00")) is None

    def test_one_cent_above_threshold_fires(self):
        hit = rules.high_amount_hit(tx("50000.01"))
        assert (hit.rule, hit.points) == ("HIGH_AMOUNT", 50)
        assert hit.detail == "Amount 50000.01 is above 50000"

    def test_threshold_comes_from_configuration(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_HIGH_AMOUNT_THRESHOLD", "1000")
        assert rules.high_amount_hit(tx("1500.00")) is not None


class TestCrossBorder:
    def test_home_country_does_not_fire(self):
        assert rules.cross_border_hit(tx(country="ZA")) is None

    def test_other_country_fires(self):
        hit = rules.cross_border_hit(tx(country="GB"))
        assert (hit.rule, hit.points, hit.detail) == ("CROSS_BORDER", 20, "Country GB is not ZA")


class TestVelocity:
    def test_ten_in_window_does_not_fire(self):
        assert rules.velocity_hit(10) is None

    def test_eleven_in_window_fires(self):
        hit = rules.velocity_hit(11)
        assert (hit.rule, hit.points) == ("HIGH_VELOCITY", 30)
        assert hit.detail == "11 transactions on the account within 60 minutes"


class TestCombinations:
    @pytest.mark.parametrize(
        "amount, country, in_window, expected_score, expected_level, expected_rules",
        [
            ("100.00", "ZA", 1, 0, "LOW", []),
            ("100.00", "GB", 1, 20, "LOW", ["CROSS_BORDER"]),
            ("100.00", "ZA", 11, 30, "LOW", ["HIGH_VELOCITY"]),
            ("60000.00", "ZA", 1, 50, "MEDIUM", ["HIGH_AMOUNT"]),
            ("100.00", "GB", 11, 50, "MEDIUM", ["HIGH_VELOCITY", "CROSS_BORDER"]),
            ("60000.00", "GB", 1, 70, "HIGH", ["HIGH_AMOUNT", "CROSS_BORDER"]),
            ("60000.00", "ZA", 11, 80, "HIGH", ["HIGH_AMOUNT", "HIGH_VELOCITY"]),
            ("60000.00", "GB", 11, 100, "HIGH", ["HIGH_AMOUNT", "HIGH_VELOCITY", "CROSS_BORDER"]),
        ],
    )
    def test_every_combination(self, amount, country, in_window, expected_score, expected_level, expected_rules):
        transaction = tx(amount, country)
        score = rules.build_score(transaction, rules.all_hits(transaction, in_window))
        assert score.risk_score == expected_score
        assert score.risk_level == expected_level
        assert [hit.rule for hit in score.rules] == expected_rules

    def test_score_is_the_sum_of_its_rule_points(self):
        transaction = tx("60000.00", "GB")
        score = rules.build_score(transaction, rules.all_hits(transaction, 11))
        assert score.risk_score == sum(hit.points for hit in score.rules)

    def test_score_never_exceeds_100(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_HIGH_AMOUNT_POINTS", "90")
        transaction = tx("60000.00", "GB")
        assert rules.total_points(rules.all_hits(transaction, 11)) == 100

    def test_pre_score_uses_only_rules_that_need_no_history(self):
        hits = rules.stateless_hits(tx("60000.00", "GB"))
        assert [hit.rule for hit in hits] == ["HIGH_AMOUNT", "CROSS_BORDER"]

    def test_score_carries_the_transaction_identity(self):
        transaction = tx()
        score = rules.build_score(transaction, ())
        assert score.transaction_id == transaction.transaction_id
        assert score.account_id == "ACC-1001"
        assert score.tx_timestamp == "2026-10-01T07:30:00Z"


class TestClassify:
    @pytest.mark.parametrize("score, level", [(0, "LOW"), (39, "LOW"), (40, "MEDIUM"), (69, "MEDIUM"),
                                              (70, "HIGH"), (100, "HIGH")])
    def test_boundaries(self, score, level):
        assert rules.classify(score) == level

    def test_thresholds_come_from_configuration(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_HIGH_RISK_SCORE", "50")
        assert rules.classify(50) == "HIGH"
