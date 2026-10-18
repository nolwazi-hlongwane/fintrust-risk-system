"""Unit tests: sorting and filtering on /transactions, /alerts and /accounts (Week 15 Day 2).

The person chooses a sort by NAME from a fixed list; the SQL for each name is
written in query.py. These tests check the list is enforced, that every
filter value travels as a parameter (never inside the SQL text), and that the
validation messages are clear.
"""

import json
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from fintrust import db, query
from tests.conftest import signed_in


class Context:
    aws_request_id = "lambda-request-id"


def call(resource, params=None):
    response = query.handler({"httpMethod": "GET", "resource": resource, "queryStringParameters": params,
                              "pathParameters": None, "requestContext": signed_in("api-request-id")}, Context())
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture(autouse=True)
def no_ssm(monkeypatch):
    monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")


@pytest.fixture
def cursor(monkeypatch):
    fake = MagicMock()
    fake.fetchone.return_value = {"total": 0}
    fake.fetchall.return_value = []

    @contextmanager
    def transaction():
        yield fake

    monkeypatch.setattr(db, "transaction", transaction)
    return fake


def list_query(cursor):
    """The second statement is the page of rows (the first is the count)."""
    return cursor.execute.call_args_list[1].args


class TestTransactionSorting:
    @pytest.mark.parametrize("sort, order_by", [
        ("newest", "ORDER BY tx_date DESC, transaction_id DESC"),
        ("oldest", "ORDER BY tx_date ASC, transaction_id ASC"),
        ("amount_desc", "ORDER BY amount DESC, tx_date DESC, transaction_id DESC"),
        ("amount_asc", "ORDER BY amount ASC, tx_date DESC, transaction_id DESC"),
        ("account_asc", "ORDER BY account_id ASC, tx_date DESC"),
        ("merchant_asc", "ORDER BY lower(merchant_name) ASC NULLS LAST"),
        ("merchant_desc", "ORDER BY lower(merchant_name) DESC NULLS LAST"),
        ("score_desc", "ORDER BY risk_score DESC"),
        ("AMOUNT_DESC", "ORDER BY amount DESC"),
    ])
    def test_each_sort_uses_its_own_fixed_order(self, cursor, sort, order_by):
        status, body = call("/transactions", {"sort": sort})
        assert status == 200 and body["sort"] == sort.lower()
        assert order_by in list_query(cursor)[0]

    def test_newest_first_is_the_default(self, cursor):
        assert call("/transactions")[1]["sort"] == "newest"
        assert "ORDER BY tx_date DESC, transaction_id DESC" in list_query(cursor)[0]

    @pytest.mark.parametrize("sort", ["amount; DROP TABLE transactions", "risk_score", "amount_desc,1", "random"])
    def test_anything_else_is_400_and_nothing_runs(self, cursor, sort):
        status, body = call("/transactions", {"sort": sort})
        assert status == 400 and body["error"]["message"].startswith("sort must be one of newest, oldest")
        cursor.execute.assert_not_called()

    def test_every_order_ends_with_a_unique_column_so_pages_never_overlap(self):
        for order_by in query.TRANSACTION_SORTS.values():
            assert order_by.rstrip().endswith(("transaction_id DESC", "transaction_id ASC"))
        for order_by in query.ALERT_SORTS.values():
            assert order_by.endswith(("a.alert_id DESC", "a.alert_id ASC"))
        for order_by in query.ACCOUNT_SORTS.values():
            assert "account_id" in order_by.split(",")[-1]


class TestTransactionFilters:
    def test_every_filter_is_a_parameter_never_part_of_the_sql(self, cursor):
        call("/transactions", {"tx_type": "debit", "channel": "online", "country_code": "gb",
                               "min_amount": "50000", "max_amount": "250000.50", "merchant": "Karoo"})
        sql, values = list_query(cursor)
        for condition in ("tx_type = %s", "channel = %s", "country_code = %s", "amount >= %s", "amount <= %s",
                          "merchant_name ILIKE %s ESCAPE '\\'"):
            assert condition in sql
        assert values[:6] == ["DEBIT", "ONLINE", "GB", Decimal("50000"), Decimal("250000.50"), "%Karoo%"]
        assert "Karoo" not in sql and "GB" not in sql and "50000" not in sql

    @pytest.mark.parametrize("search, pattern", [
        ("100%", "%100\\%%"),            # a percent sign is a literal, not "anything"
        ("a_b", "%a\\_b%"),              # so is an underscore
        ("back\\slash", "%back\\\\slash%"),
        ("  Acme  ", "%Acme%"),
    ])
    def test_merchant_search_escapes_like_wildcards(self, cursor, search, pattern):
        call("/transactions", {"merchant": search})
        assert list_query(cursor)[1][0] == pattern

    def test_digits_are_short_for_an_account_id(self, cursor):
        call("/transactions", {"account_id": "1007"})
        assert list_query(cursor)[1][0] == "ACC-1007"

    @pytest.mark.parametrize("params, message", [
        ({"tx_type": "REFUND"}, "tx_type must be one of CREDIT, DEBIT"),
        ({"channel": "FAX"}, "channel must be one of POS, ONLINE, ATM, EFT, MOBILE, BRANCH"),
        ({"country_code": "ZAF"}, "country_code must be two letters, for example ZA or GB"),
        ({"min_amount": "lots"}, "min_amount must be a number, for example 50000 or 1250.50"),
        ({"min_amount": "-1"}, "min_amount must be between 0 and 999999999999.99 with at most two decimals"),
        ({"max_amount": "10.005"}, "max_amount must be between 0 and 999999999999.99 with at most two decimals"),
        ({"max_amount": "NaN"}, "max_amount must be between 0 and 999999999999.99 with at most two decimals"),
        ({"min_amount": "500", "max_amount": "100"}, "min_amount must not be more than max_amount"),
        ({"merchant": "x" * 61}, "merchant must be at most 60 characters"),
        ({"merchant": "line\nbreak"}, "merchant contains characters that are not allowed"),
    ])
    def test_bad_filters_are_400_and_nothing_runs(self, cursor, params, message):
        status, body = call("/transactions", params)
        assert status == 400 and body["error"]["message"] == message
        cursor.execute.assert_not_called()

    def test_amounts_may_be_written_with_spaces(self, cursor):
        call("/transactions", {"min_amount": "50 000"})
        assert list_query(cursor)[1][0] == Decimal("50000")


class TestAlerts:
    def test_filters_and_sort(self, cursor):
        status, body = call("/alerts", {"status": "open", "account_id": "ACC-1007", "from": "2026-09-01",
                                        "to": "2026-09-30", "sort": "amount_desc"})
        assert status == 200 and body["sort"] == "amount_desc"
        sql, values = list_query(cursor)
        assert "a.status = %s" in sql and "t.account_id = %s" in sql and "a.created_at >= %s" in sql
        assert "ORDER BY t.amount DESC, a.created_at DESC, a.alert_id DESC" in sql
        assert values[:2] == ["OPEN", "ACC-1007"]

    def test_the_count_uses_the_same_filters_as_the_page(self, cursor):
        call("/alerts", {"account_id": "ACC-1007"})
        count_sql, count_values = cursor.execute.call_args_list[0].args
        assert "JOIN transactions t" in count_sql and count_values == ["ACC-1007"]

    def test_unknown_sort_is_400(self, cursor):
        assert call("/alerts", {"sort": "merchant_asc"})[0] == 400


class TestAccounts:
    def test_filters_and_sort(self, cursor):
        status, body = call("/accounts", {"status": "flagged", "account_type": "business", "search": "10",
                                          "sort": "recent"})
        assert status == 200 and body["sort"] == "recent"
        sql, values = list_query(cursor)
        assert "account_status = %s" in sql and "account_type = %s" in sql and "account_id ILIKE %s" in sql
        assert "ORDER BY last_transaction_at DESC NULLS LAST, account_id" in sql
        assert values[:3] == ["FLAGGED", "BUSINESS", "%10%"]

    def test_highest_risk_first_is_the_default(self, cursor):
        assert call("/accounts")[1]["sort"] == "risk_desc"

    @pytest.mark.parametrize("params", [{"account_type": "LOAN"}, {"sort": "name"}, {"search": "x" * 61}])
    def test_bad_values_are_400(self, cursor, params):
        assert call("/accounts", params)[0] == 400
