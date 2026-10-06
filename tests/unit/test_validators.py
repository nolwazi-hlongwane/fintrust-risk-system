"""Unit tests for fintrust.validators."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from fintrust import validators
from fintrust.validators import mask_account, sanitise_text, validate_header, validate_row

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def row(**overrides):
    values = {
        "transaction_id": "TXN-20261001-900001",
        "account_id": "ACC-1001",
        "tx_date": "2026-10-01T09:30:00+02:00",
        "amount": "412.50",
        "currency": "ZAR",
        "tx_type": "DEBIT",
        "channel": "POS",
        "merchant_name": "Checkers Sandton",
        "country_code": "ZA",
        "description": "Groceries",
    }
    values.update(overrides)
    return values


def check(**overrides):
    return validate_row(row(**overrides), "uploads/test.csv", now=NOW)


class TestValidRow:
    def test_clean_row_becomes_a_transaction(self):
        result = check()
        assert result.is_valid
        assert result.errors == () and result.warnings == ()
        transaction = result.transaction
        assert transaction.transaction_id == "TXN-20261001-900001"
        assert transaction.amount == Decimal("412.50")
        assert transaction.source_file == "uploads/test.csv"
        assert transaction.tx_timestamp == "2026-10-01T07:30:00Z"

    def test_values_are_trimmed_and_upper_cased(self):
        result = check(account_id=" acc-1001 ", tx_type="debit", channel=" pos", country_code="za", currency="zar")
        transaction = result.transaction
        assert (transaction.account_id, transaction.tx_type, transaction.channel) == ("ACC-1001", "DEBIT", "POS")
        assert (transaction.country_code, transaction.currency) == ("ZA", "ZAR")

    def test_currency_defaults_to_rand(self):
        assert check(currency="").transaction.currency == "ZAR"

    def test_optional_text_fields_may_be_empty(self):
        transaction = check(merchant_name="", description=None).transaction
        assert transaction.merchant_name is None and transaction.description is None

    def test_amount_is_stored_with_two_decimals(self):
        assert str(check(amount="75000").transaction.amount) == "75000.00"
        assert str(check(amount="0.5").transaction.amount) == "0.50"


class TestRequiredFields:
    @pytest.mark.parametrize("field", validators.REQUIRED_COLUMNS)
    def test_each_required_field(self, field):
        result = check(**{field: "  "})
        assert not result.is_valid
        assert f"{field} is required" in result.errors

    def test_missing_key_is_treated_as_blank(self):
        values = row()
        del values["amount"]
        assert "amount is required" in validate_row(values, "f.csv", now=NOW).errors

    def test_every_problem_is_reported_not_just_the_first(self):
        result = check(account_id="1011", tx_date="2099-01-01T00:00:00+02:00", amount="abc", currency="RAND",
                       channel="WIRE", country_code="South Africa")
        assert len(result.errors) == 6


class TestIdentifiers:
    @pytest.mark.parametrize("value", ["TXN-1", "TXN 20261001 1", "TXN_20261001", "-TXN-20261001", "T" * 41])
    def test_bad_transaction_id(self, value):
        assert "transaction_id must be 6 to 40 letters, digits or dashes" in check(transaction_id=value).errors

    @pytest.mark.parametrize("value", ["1001", "ACC1001", "ACC-12", "ACC-10A1", "ACCT-1001"])
    def test_bad_account_id(self, value):
        assert "account_id must look like ACC-1234" in check(account_id=value).errors


class TestAmount:
    @pytest.mark.parametrize("value", ["-150.00", "1,250.00", "R100", "100.999", "abc", "1e5", "+5", ".50", "NaN"])
    def test_bad_format(self, value):
        result = check(amount=value)
        assert not result.is_valid
        assert any("amount" in error for error in result.errors)

    def test_zero_is_rejected(self):
        assert "amount must be greater than zero" in check(amount="0.00").errors

    def test_too_large_for_the_ledger(self):
        assert any("larger than the ledger" in error for error in check(amount="1000000000000.00").errors)

    def test_largest_storable_amount_is_accepted(self):
        assert check(amount="999999999999.99").is_valid


class TestDate:
    def test_utc_z_suffix(self):
        assert check(tx_date="2026-10-01T07:30:00Z").transaction.tx_timestamp == "2026-10-01T07:30:00Z"

    def test_space_separator(self):
        assert check(tx_date="2026-10-01 09:30:00+02:00").is_valid

    def test_no_timezone_is_read_as_south_african_time(self):
        assert check(tx_date="2026-10-01 09:30:00").transaction.tx_timestamp == "2026-10-01T07:30:00Z"

    @pytest.mark.parametrize("value", ["05/10/2026 10:00", "2026-13-01T00:00:00", "yesterday", "2026/10/01 09:00"])
    def test_unreadable_date(self, value):
        assert any("not a valid date and time" in error for error in check(tx_date=value).errors)

    @pytest.mark.parametrize("value", ["2026-10-01", "20261001"])
    def test_date_without_time(self, value):
        assert any("no time of day" in error for error in check(tx_date=value).errors)

    def test_future_date_is_rejected(self):
        assert "tx_date is in the future" in check(tx_date="2026-10-06T14:30:00+00:00").errors

    def test_a_few_minutes_of_clock_difference_is_tolerated(self):
        assert check(tx_date="2026-10-06T12:03:00+00:00").is_valid

    def test_very_old_date_is_rejected(self):
        assert "tx_date is before the year 2000" in check(tx_date="1999-12-31T23:59:59+00:00").errors


class TestAllowedValues:
    def test_unknown_type(self):
        assert "tx_type must be one of CREDIT, DEBIT" in check(tx_type="REFUND").errors

    def test_unknown_channel(self):
        assert any(error.startswith("channel must be one of") for error in check(channel="WIRE").errors)

    @pytest.mark.parametrize("value", ["ZAF", "Z", "Z1", "South Africa"])
    def test_bad_country(self, value):
        assert "country_code must be a 2-letter code such as ZA" in check(country_code=value).errors

    @pytest.mark.parametrize("value", ["RAND", "R", "ZA1"])
    def test_bad_currency(self, value):
        assert "currency must be a 3-letter code such as ZAR" in check(currency=value).errors

    def test_allowed_values_match_the_database_constraints(self):
        from pathlib import Path

        schema = (Path(__file__).resolve().parents[2] / "sql" / "schema.sql").read_text()
        for channel in validators.CHANNELS:
            assert f"'{channel}'" in schema
        for tx_type in validators.TX_TYPES:
            assert f"'{tx_type}'" in schema


class TestSanitisation:
    def test_control_characters_and_extra_spaces_are_removed(self):
        text, warnings = sanitise_text("  Pick\tn\n Pay \x00 Rosebank  ", "merchant_name", 120)
        assert text == "Pick n Pay Rosebank"
        assert warnings == []

    def test_only_whitespace_becomes_none(self):
        assert sanitise_text(" \t\n ", "description", 255) == (None, [])

    @pytest.mark.parametrize("value", ["=HYPERLINK(1)", "+SUM(A1)", "-2+3", "@cmd"])
    def test_formula_characters_are_neutralised(self, value):
        text, warnings = sanitise_text(value, "description", 255)
        assert text == "'" + value
        assert warnings == ["description started with a formula character and was neutralised"]

    def test_long_text_is_shortened_with_a_warning(self):
        text, warnings = sanitise_text("x" * 300, "description", 255)
        assert len(text) == 255
        assert "shortened" in warnings[0]

    def test_sql_text_is_kept_as_harmless_text(self):
        result = check(description="x'; DROP TABLE transactions; --")
        assert result.is_valid
        assert result.transaction.description == "x'; DROP TABLE transactions; --"

    def test_ordinary_text_with_short_numbers_is_untouched(self):
        text, warnings = sanitise_text("Invoice 20261001 ref 4471 for R1500", "description", 255)
        assert text == "Invoice 20261001 ref 4471 for R1500"
        assert warnings == []


class TestPopiaRedaction:
    def test_email_address_is_redacted(self):
        result = check(description="Paid for j.naidoo@example.com")
        assert result.transaction.description == "Paid for [REDACTED]"
        assert result.warnings == ("description contained a possible email address, which was redacted",)

    def test_id_number_is_redacted(self):
        result = check(description="ID 8001015009087 verified")
        assert result.transaction.description == "ID [REDACTED] verified"

    @pytest.mark.parametrize("value", ["4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111"])
    def test_card_number_is_redacted(self, value):
        assert check(description=f"card {value} used").transaction.description == "card [REDACTED] used"

    @pytest.mark.parametrize("value", ["0825550101", "082 555 0101", "+27 82 555 0101", "+27825550101"])
    def test_phone_number_is_redacted(self, value):
        assert check(description=f"call {value}").transaction.description == "call [REDACTED]"

    def test_redaction_applies_to_merchant_name_too(self):
        assert check(merchant_name="Shop owner@shop.co.za").transaction.merchant_name == "Shop [REDACTED]"

    def test_row_with_redaction_is_still_accepted(self):
        assert check(description="mail a@b.co").is_valid

    def test_nothing_personal_survives_in_the_queue_message(self):
        result = check(description="j.naidoo@example.com 8001015009087 082 555 0101")
        message = str(result.transaction.to_message())
        for secret in ("j.naidoo", "8001015009087", "0101"):
            assert secret not in message


class TestMaskAccount:
    @pytest.mark.parametrize("value, expected", [("ACC-1007", "ACC-**07"), ("ACC-123456", "ACC-****56"),
                                                 ("12345678", "******78"), ("A", "**"), ("", "**"), (None, "**")])
    def test_mask(self, value, expected):
        assert mask_account(value) == expected


class TestHeader:
    GOOD = ["transaction_id", "account_id", "tx_date", "amount", "currency", "tx_type", "channel",
            "merchant_name", "country_code", "description"]

    def test_good_header(self):
        assert validate_header(self.GOOD) == []

    def test_case_spaces_and_byte_order_mark_are_ignored(self):
        assert validate_header(["﻿Transaction_ID", " Account_Id ", "TX_DATE", "Amount", "tx_type", "channel",
                                "country_code"]) == []

    def test_merchant_alias_is_accepted(self):
        header = [name if name != "merchant_name" else "merchant" for name in self.GOOD]
        assert "merchant_name" in validators.normalise_header(header)

    def test_missing_columns_are_named(self):
        problems = validate_header(["transaction_id", "amount"])
        assert problems == ["missing required column(s): account_id, tx_date, tx_type, channel, country_code"]

    def test_duplicate_columns(self):
        assert "duplicate column(s): amount" in validate_header(self.GOOD + ["amount"])

    def test_empty_header(self):
        assert validate_header([]) != [] and validate_header(None) != []

    def test_unneeded_personal_columns_are_reported_as_ignored(self):
        assert validators.ignored_columns(self.GOOD + ["customer_name", "ID_Number"]) == ["customer_name", "id_number"]

    def test_unneeded_columns_never_reach_the_transaction(self):
        result = validate_row(row(customer_name="Thandiwe Mokoena", id_number="8001015009087"), "f.csv", now=NOW)
        assert "Thandiwe" not in str(result.transaction)
        assert "8001015009087" not in str(result.transaction)
