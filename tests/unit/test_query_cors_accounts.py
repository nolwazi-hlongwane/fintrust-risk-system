"""Unit tests for browser access (CORS) and the accounts route's validation."""

import json

import pytest

from fintrust import query
from tests.unit.test_query import Context, api_event, call
from tests.unit.test_query import no_ssm as no_ssm  # noqa: F401  (autouse fixture)

DASHBOARD = "https://d111111abcdef8.cloudfront.net"


def origin_for(request_origin, header_name="origin"):
    event = api_event("GET", "/nope")
    if request_origin is not None:
        event["headers"] = {header_name: request_origin}
    return query.handler(event, Context())["headers"]


class TestCors:
    def test_before_the_dashboard_exists_any_origin_is_allowed(self):
        assert origin_for("https://anything.example")["Access-Control-Allow-Origin"] == "*"

    def test_the_dashboard_origin_is_echoed_back(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_CORS_ALLOWED_ORIGIN", DASHBOARD)
        assert origin_for(DASHBOARD)["Access-Control-Allow-Origin"] == DASHBOARD

    def test_another_website_is_not_echoed_back(self, monkeypatch):
        """The browser compares the header with the page's own address; a mismatch blocks the page from reading."""
        monkeypatch.setenv("FINTRUST_CORS_ALLOWED_ORIGIN", DASHBOARD)
        headers = origin_for("https://evil.example")
        assert headers["Access-Control-Allow-Origin"] == DASHBOARD
        assert "evil.example" not in json.dumps(headers)

    @pytest.mark.parametrize("sneaky", [
        DASHBOARD + ".evil.example", "http://d111111abcdef8.cloudfront.net", DASHBOARD + "/", "null", ""])
    def test_lookalike_origins_are_not_accepted(self, monkeypatch, sneaky):
        monkeypatch.setenv("FINTRUST_CORS_ALLOWED_ORIGIN", DASHBOARD)
        assert origin_for(sneaky)["Access-Control-Allow-Origin"] == DASHBOARD

    def test_several_origins_can_be_allowed(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_CORS_ALLOWED_ORIGIN", f"{DASHBOARD}, http://localhost:8080")
        assert origin_for("http://localhost:8080")["Access-Control-Allow-Origin"] == "http://localhost:8080"
        assert origin_for(DASHBOARD)["Access-Control-Allow-Origin"] == DASHBOARD
        assert origin_for("http://localhost:9999")["Access-Control-Allow-Origin"] == DASHBOARD

    def test_header_name_is_matched_whatever_its_capitalisation(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_CORS_ALLOWED_ORIGIN", f"{DASHBOARD},http://localhost:8080")
        assert origin_for("http://localhost:8080", "Origin")["Access-Control-Allow-Origin"] == "http://localhost:8080"

    def test_request_without_an_origin_such_as_curl_still_works(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_CORS_ALLOWED_ORIGIN", DASHBOARD)
        assert origin_for(None)["Access-Control-Allow-Origin"] == DASHBOARD

    def test_responses_vary_by_origin_so_caches_do_not_mix_sites(self):
        assert origin_for(DASHBOARD)["Vary"] == "Origin"

    def test_preflight_and_errors_carry_the_same_header(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_CORS_ALLOWED_ORIGIN", DASHBOARD)
        for method, resource in (("OPTIONS", "/upload"), ("GET", "/nope"), ("POST", "/flag")):
            event = api_event(method, resource)
            event["headers"] = {"origin": DASHBOARD}
            assert query.handler(event, Context())["headers"]["Access-Control-Allow-Origin"] == DASHBOARD


class TestAccountsValidation:
    def test_unknown_status_is_400(self):
        status, body, _ = call("GET", "/accounts", {"status": "FROZEN"})
        assert status == 400 and body["error"]["message"] == "status must be one of ACTIVE, FLAGGED, CLOSED"

    @pytest.mark.parametrize("params", [{"limit": "0"}, {"limit": "101"}, {"limit": "ten"}, {"offset": "-1"}])
    def test_bad_paging_is_400(self, params):
        assert call("GET", "/accounts", params)[0] == 400

    def test_sql_injection_attempt_in_status_is_refused_before_any_query(self):
        status, body, _ = call("GET", "/accounts", {"status": "ACTIVE' OR '1'='1"})
        assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"
