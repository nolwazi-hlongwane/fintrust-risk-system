"""Integration tests: the query Lambda's SQL against the seed data in a real
PostgreSQL, as the fintrust_query database user."""

import json

import pytest

from fintrust import query

pytestmark = pytest.mark.integration


class Context:
    aws_request_id = "test-request-id"


@pytest.fixture(autouse=True)
def as_query_user(as_db_user):
    as_db_user("fintrust_query")


def get(resource, params=None, path=None):
    response = query.handler(
        {"httpMethod": "GET", "resource": resource, "queryStringParameters": params, "pathParameters": path,
         "requestContext": {"requestId": "api-request-id"}},
        Context(),
    )
    return response["statusCode"], json.loads(response["body"])


class TestHealth:
    def test_health_reaches_the_database(self):
        assert get("/health") == (200, {"status": "ok", "database": "ok", "version": "0.1.0"})


class TestTransactions:
    def test_default_page(self):
        status, body = get("/transactions")
        assert status == 200
        assert len(body["items"]) == 25
        assert body["pagination"] == {"limit": 25, "offset": 0, "total": 275, "has_more": True}

    def test_newest_first(self):
        dates = [item["tx_date"] for item in get("/transactions", {"limit": "100"})[1]["items"]]
        assert dates == sorted(dates, reverse=True)

    def test_filter_by_account(self):
        status, body = get("/transactions", {"account_id": "ACC-1007", "limit": "100"})
        assert status == 200
        assert body["pagination"]["total"] == 31
        assert {item["account_id"] for item in body["items"]} == {"ACC-1007"}

    def test_account_filter_is_case_insensitive(self):
        assert get("/transactions", {"account_id": "acc-1007"})[1]["pagination"]["total"] == 31

    def test_filter_by_risk_level(self):
        body = get("/transactions", {"risk_level": "HIGH", "limit": "100"})[1]
        assert body["pagination"]["total"] == 12
        assert all(item["risk_score"] >= 70 for item in body["items"])

    def test_filters_combine(self):
        body = get("/transactions", {"account_id": "ACC-1007", "risk_level": "HIGH"})[1]
        assert body["pagination"]["total"] == 5

    def test_filter_by_date_range_is_inclusive_in_south_african_time(self):
        body = get("/transactions", {"from": "2026-09-05", "to": "2026-09-05", "account_id": "ACC-1003"})[1]
        assert body["pagination"]["total"] == 14          # the loan credit and the 13-transaction burst
        assert all(item["tx_date"].startswith("2026-09-05") for item in body["items"])

    def test_filter_by_transaction_id(self):
        any_id = get("/transactions", {"limit": "1"})[1]["items"][0]["transaction_id"]
        body = get("/transactions", {"transaction_id": any_id})[1]
        assert [item["transaction_id"] for item in body["items"]] == [any_id]

    def test_pagination_walks_every_row_exactly_once(self):
        seen = []
        for offset in range(0, 300, 100):
            seen += [item["transaction_id"] for item in get("/transactions", {"limit": "100", "offset": str(offset)})[1]["items"]]
        assert len(seen) == 275 and len(set(seen)) == 275

    def test_last_page(self):
        body = get("/transactions", {"limit": "100", "offset": "200"})[1]
        assert len(body["items"]) == 75 and body["pagination"]["has_more"] is False

    def test_offset_beyond_the_end_is_an_empty_page(self):
        body = get("/transactions", {"offset": "5000"})[1]
        assert body["items"] == [] and body["pagination"]["total"] == 275

    def test_unknown_account_is_an_empty_list_not_an_error(self):
        status, body = get("/transactions", {"account_id": "ACC-0000"})
        assert status == 200 and body["pagination"]["total"] == 0

    def test_amount_is_exact_text(self):
        body = get("/transactions", {"account_id": "ACC-1007", "risk_level": "HIGH", "limit": "100"})[1]
        assert "104500.00" in [item["amount"] for item in body["items"]]

    def test_response_has_no_customer_details(self):
        text = json.dumps(get("/transactions", {"limit": "100"})[1])
        assert "example.com" not in text and "+27" not in text

    def test_injection_attempt_is_rejected_before_reaching_the_database(self):
        status, _ = get("/transactions", {"account_id": "ACC-1007'; DROP TABLE alerts; --"})
        assert status == 400
        assert get("/insights")[1]["open_alerts"] == 3


class TestInsights:
    def test_whole_ledger(self):
        status, body = get("/insights")
        assert status == 200
        assert body["period"] == {"from": None, "to": None}
        assert body["totals"]["transactions"] == 275
        assert body["totals"]["accounts"] == 12
        assert body["by_risk_level"] == {"LOW": 244, "MEDIUM": 19, "HIGH": 12}
        assert body["rules_fired"] == {"HIGH_AMOUNT": 30, "HIGH_VELOCITY": 4, "CROSS_BORDER": 19}
        assert body["open_alerts"] == 3

    def test_levels_add_up_to_the_total(self):
        body = get("/insights")[1]
        assert sum(body["by_risk_level"].values()) == body["totals"]["transactions"]

    def test_total_value_matches_the_ledger(self, admin):
        with admin.cursor() as cursor:
            cursor.execute("SELECT sum(amount)::text FROM transactions")
            expected = cursor.fetchone()[0]
        assert get("/insights")[1]["totals"]["total_value"] == expected

    def test_top_risk_accounts(self):
        top = get("/insights")[1]["top_risk_accounts"]
        assert top == [
            {"account_id": "ACC-1007", "high_risk_transactions": 5, "status": "FLAGGED"},
            {"account_id": "ACC-1005", "high_risk_transactions": 4, "status": "ACTIVE"},
            {"account_id": "ACC-1011", "high_risk_transactions": 2, "status": "ACTIVE"},
            {"account_id": "ACC-1003", "high_risk_transactions": 1, "status": "ACTIVE"},
        ]

    def test_period_filter(self):
        july = get("/insights", {"from": "2026-07-01", "to": "2026-07-31"})[1]
        august = get("/insights", {"from": "2026-08-01", "to": "2026-08-31"})[1]
        september = get("/insights", {"from": "2026-09-01", "to": "2026-09-30"})[1]
        assert july["period"] == {"from": "2026-07-01", "to": "2026-07-31"}
        totals = [month["totals"]["transactions"] for month in (july, august, september)]
        assert sum(totals) == 275 and all(total > 0 for total in totals)
        assert july["by_risk_level"]["HIGH"] + august["by_risk_level"]["HIGH"] + september["by_risk_level"]["HIGH"] == 12

    def test_period_with_no_data(self):
        body = get("/insights", {"from": "2020-01-01", "to": "2020-01-31"})[1]
        assert body["totals"] == {"transactions": 0, "total_value": "0", "accounts": 0}
        assert body["top_risk_accounts"] == []


class TestJourneyFromLedger:
    def test_seed_transaction_has_its_rule_breakdown(self, events_table):
        status, body = get("/transactions/{transaction_id}/journey", path={"transaction_id": "TXN-20260915-000225"})
        assert status == 200 and body["source"] == "ledger"
        assert (body["account_id"], body["score"]["risk_score"], body["score"]["risk_level"]) == ("ACC-1007", 70, "HIGH")
        assert [(r["rule"], r["points"]) for r in body["score"]["rules"]] == [("HIGH_AMOUNT", 50), ("CROSS_BORDER", 20)]
        assert sum(r["points"] for r in body["score"]["rules"]) == body["score"]["risk_score"]

    def test_low_risk_seed_transaction_has_no_rules(self, events_table):
        _, body = get("/transactions/{transaction_id}/journey", path={"transaction_id": "TXN-20260701-000005"})
        assert body["score"]["risk_score"] == 0 and body["score"]["rules"] == []

    def test_transaction_in_neither_store_is_404(self, events_table):
        status, _ = get("/transactions/{transaction_id}/journey", path={"transaction_id": "TXN-NOPE-000001"})
        assert status == 404


class TestAccounts:
    def test_accounts_are_ranked_by_total_risk_points(self):
        status, body = get("/accounts")
        assert status == 200 and body["pagination"]["total"] == 12
        points = [item["total_risk_points"] for item in body["items"]]
        assert points == sorted(points, reverse=True)
        assert [item["account_id"] for item in body["items"][:2]] == ["ACC-1005", "ACC-1007"]

    def test_row_carries_the_whole_risk_picture(self):
        _, body = get("/accounts", {"status": "FLAGGED"})
        assert [item["account_id"] for item in body["items"]] == ["ACC-1007"]
        row = body["items"][0]
        assert (row["high_risk_count"], row["transaction_count"], row["manual_flags"]) == (5, 31, 1)
        assert row["open_alerts"] + row["resolved_alerts"] == 5
        assert row["account_type"] == "BUSINESS"

    def test_no_customer_details_are_returned(self):
        _, body = get("/accounts", {"limit": "100"})
        assert not {"customer_id", "full_name", "email", "phone_number"} & set(body["items"][0])

    def test_paging(self):
        _, first = get("/accounts", {"limit": "5"})
        _, second = get("/accounts", {"limit": "5", "offset": "5"})
        assert len(first["items"]) == 5 and first["pagination"]["has_more"] is True
        assert not {i["account_id"] for i in first["items"]} & {i["account_id"] for i in second["items"]}


class TestDatabasePermissions:
    def test_query_lambda_cannot_change_the_ledger(self):
        from fintrust import db
        from fintrust.db import DatabaseError

        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute("DELETE FROM transactions")
        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute("UPDATE transactions SET risk_score = 0")
