"""Integration tests: sorting and filtering against a real PostgreSQL (Week 15 Day 2).

Each test asks the API for a sorted or filtered list and checks the answer
against the same rows worked out independently in Python, so the order is
proved correct across the whole table, not only on one page.
"""

import json
from decimal import Decimal

import pytest

from fintrust import query
from tests.conftest import signed_in

pytestmark = pytest.mark.integration


class Context:
    aws_request_id = "test-request-id"


@pytest.fixture(autouse=True)
def as_query_user(as_db_user):
    as_db_user("fintrust_query")


def get(resource, params=None):
    response = query.handler({"httpMethod": "GET", "resource": resource, "queryStringParameters": params,
                              "pathParameters": None, "requestContext": signed_in("api-request-id")}, Context())
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def every_page(resource, params):
    """All rows, by following the pages the way the dashboard does."""
    rows, offset = [], 0
    while True:
        page = get(resource, {**params, "limit": "100", "offset": str(offset)})
        rows += page["items"]
        if not page["pagination"]["has_more"]:
            return rows
        offset += 100


def rows(admin, sql):
    with admin.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


class TestTransactions:
    def test_amount_high_to_low_across_every_page(self, admin):
        found = every_page("/transactions", {"sort": "amount_desc"})
        amounts = [Decimal(item["amount"]) for item in found]
        assert len(found) == rows(admin, "SELECT count(*) FROM transactions")[0][0]
        assert amounts == sorted(amounts, reverse=True)
        assert len({item["transaction_id"] for item in found}) == len(found)      # no row on two pages

    def test_amount_low_to_high(self, admin):
        amounts = [Decimal(item["amount"]) for item in every_page("/transactions", {"sort": "amount_asc"})]
        assert amounts == sorted(amounts)

    def test_oldest_first_and_newest_first_are_reverses_of_each_other(self, admin):
        oldest = [item["transaction_id"] for item in every_page("/transactions", {"sort": "oldest"})]
        newest = [item["transaction_id"] for item in every_page("/transactions", {"sort": "newest"})]
        assert oldest == list(reversed(newest))

    def test_merchant_a_to_z_ignores_case_and_puts_empty_last(self, admin):
        names = [item["merchant_name"] for item in every_page("/transactions", {"sort": "merchant_asc"})]
        named = [name for name in names if name]
        assert named == sorted(named, key=str.lower)
        assert names[len(named):] == [None] * (len(names) - len(named))

    def test_account_a_to_z(self, admin):
        accounts = [item["account_id"] for item in every_page("/transactions", {"sort": "account_asc"})]
        assert accounts == sorted(accounts)

    def test_amount_range_and_type_filters(self, admin):
        found = every_page("/transactions", {"min_amount": "50000", "max_amount": "150000", "tx_type": "DEBIT"})
        expected = rows(admin, "SELECT count(*) FROM transactions WHERE amount BETWEEN 50000 AND 150000 "
                               "AND tx_type = 'DEBIT'")[0][0]
        assert len(found) == expected > 0
        assert all(Decimal("50000") <= Decimal(item["amount"]) <= Decimal("150000") for item in found)

    def test_country_and_channel_filters(self, admin):
        found = every_page("/transactions", {"country_code": "gb"})
        assert found and {item["country_code"] for item in found} == {"GB"}
        channel = rows(admin, "SELECT channel FROM transactions LIMIT 1")[0][0]
        assert {item["channel"] for item in every_page("/transactions", {"channel": channel})} == {channel}

    def test_merchant_search_is_case_insensitive_and_partial(self, admin):
        merchant = rows(admin, "SELECT merchant_name FROM transactions WHERE merchant_name IS NOT NULL "
                               "ORDER BY merchant_name LIMIT 1")[0][0]
        part = merchant[1:5].upper()
        found = every_page("/transactions", {"merchant": part})
        assert found and all(part.lower() in item["merchant_name"].lower() for item in found)

    def test_a_percent_sign_matches_only_a_percent_sign(self, admin):
        """Unescaped, "%" would match every row. Escaped, it matches merchants with a "%" in the name."""
        assert get("/transactions", {"merchant": "%"})["pagination"]["total"] == rows(
            admin, "SELECT count(*) FROM transactions WHERE merchant_name LIKE '%\\%%'")[0][0]


class TestAlerts:
    def test_largest_amount_first(self, admin):
        amounts = [Decimal(item["amount"]) for item in every_page("/alerts", {"sort": "amount_desc"})]
        assert amounts == sorted(amounts, reverse=True) and len(amounts) == 12

    def test_one_accounts_alerts(self, admin):
        found = every_page("/alerts", {"account_id": "ACC-1007"})
        expected = rows(admin, "SELECT count(*) FROM alerts a JOIN transactions t USING (transaction_id) "
                               "WHERE t.account_id = 'ACC-1007'")[0][0]
        assert len(found) == expected > 0 and {item["account_id"] for item in found} == {"ACC-1007"}


class TestAccounts:
    def test_a_to_z_and_z_to_a(self, admin):
        ascending = [item["account_id"] for item in every_page("/accounts", {"sort": "account_asc"})]
        assert ascending == sorted(ascending)
        assert [item["account_id"] for item in every_page("/accounts", {"sort": "account_desc"})] == ascending[::-1]

    def test_most_recently_active_first(self, admin):
        found = every_page("/accounts", {"sort": "recent"})
        times = [item["last_transaction_at"] for item in found if item["last_transaction_at"]]
        assert times == sorted(times, reverse=True)

    def test_search_and_type(self, admin):
        found = every_page("/accounts", {"search": "100", "account_type": "BUSINESS"})
        assert found and all("100" in item["account_id"] and item["account_type"] == "BUSINESS" for item in found)
