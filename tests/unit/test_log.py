"""Unit tests for fintrust.log."""

import json
from datetime import datetime
from decimal import Decimal

from fintrust.log import get_logger


def last_line(capsys):
    lines = capsys.readouterr().out.strip().splitlines()
    return json.loads(lines[-1])


def test_each_line_is_one_json_object(capsys):
    get_logger("ingest").info("file_received", rows=3)
    output = capsys.readouterr().out.strip()
    assert len(output.splitlines()) == 1
    assert json.loads(output)["message"] == "file_received"


def test_standard_fields_are_present(capsys):
    get_logger("score").info("scored", transaction_id="TXN-1")
    record = last_line(capsys)
    assert record["level"] == "INFO"
    assert record["service"] == "score"
    assert record["env"] == "test"
    assert record["transaction_id"] == "TXN-1"
    datetime.fromisoformat(record["timestamp"])  # raises if not a timestamp


def test_levels(capsys):
    logger = get_logger("query")
    logger.warning("slow_query")
    assert last_line(capsys)["level"] == "WARNING"
    logger.error("query_failed")
    assert last_line(capsys)["level"] == "ERROR"


def test_decimal_datetime_and_exception_do_not_crash(capsys):
    get_logger("report").error(
        "failed",
        amount=Decimal("50000.10"),
        when=datetime(2026, 9, 28, 8, 0),
        error=ValueError("bad row"),
    )
    record = last_line(capsys)
    assert record["amount"] == "50000.10"
    assert record["when"].startswith("2026-09-28")
    assert record["error"] == "bad row"


def test_bind_adds_context_to_every_line(capsys):
    logger = get_logger("ingest").bind(request_id="abc-123")
    logger.info("one")
    assert last_line(capsys)["request_id"] == "abc-123"
    logger.info("two", rows=5)
    record = last_line(capsys)
    assert record["request_id"] == "abc-123"
    assert record["rows"] == 5


def test_bind_does_not_change_the_original_logger(capsys):
    base = get_logger("ingest")
    base.bind(request_id="abc-123")
    base.info("plain")
    assert "request_id" not in last_line(capsys)


def test_debug_is_hidden_by_default(capsys):
    get_logger("ingest").debug("noisy")
    assert capsys.readouterr().out == ""


def test_debug_is_shown_when_enabled(capsys, monkeypatch):
    monkeypatch.setenv("FINTRUST_LOG_LEVEL", "DEBUG")
    get_logger("ingest").debug("noisy")
    assert last_line(capsys)["level"] == "DEBUG"
