"""Tests for the demonstration and submission tools in scripts/.

A live demonstration that behaves differently from the rehearsal is the worst
kind of failure, so the demo file is checked against the real validation and
scoring rules, and the diagrams are checked against what they are generated from.
"""

import ast
import csv
import io
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fintrust import rules, validators
from fintrust.validators import ACCOUNT_ID_PATTERN, TRANSACTION_ID_PATTERN

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import build_diagrams  # noqa: E402
import make_demo_file  # noqa: E402
import onboard_account  # noqa: E402

SAST = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 9, 10, 30, 15, tzinfo=SAST)
E2E_SOURCE = (ROOT / "scripts" / "e2e_test.py").read_text()


def e2e_constant(name):
    """Read a constant from e2e_test.py without importing it (importing it calls AWS)."""
    value = re.search(rf"^{name} = (.+?)(?=^\S)", E2E_SOURCE, re.S | re.M).group(1)
    return ast.literal_eval(value.split("\n\n")[0].strip())


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
    name, text, high_id, dead_letter_id, new_account = make_demo_file.build(NOW)
    rows = list(csv.DictReader(io.StringIO(text)))
    results = [validators.validate_row(row, name, now=NOW.astimezone(timezone.utc)) for row in rows]
    return {"name": name, "text": text, "rows": rows, "results": results, "high_id": high_id,
            "dead_letter_id": dead_letter_id, "new_account": new_account}


class TestDemoFile:
    def test_file_has_the_columns_the_pipeline_requires(self, demo):
        assert validators.validate_header(demo["text"].splitlines()[0].split(",")) == []

    def test_ten_rows_are_valid_and_two_are_rejected_with_a_reason(self, demo):
        rejected = [number for number, result in enumerate(demo["results"], start=1) if not result.is_valid]
        assert rejected == list(e2e_constant("INVALID_ROWS")) == [6, 7]
        assert all(demo["results"][number - 1].errors for number in rejected)
        assert len(demo["rows"]) == 12

    def test_every_valid_row_scores_exactly_what_the_end_to_end_test_expects(self, demo):
        """One transaction per account inside the hour, so the velocity rule cannot fire."""
        expected = e2e_constant("EXPECTED_SCORES")
        dead_letter_row = e2e_constant("DEAD_LETTER_ROW")
        for number, result in enumerate(demo["results"], start=1):
            if not result.is_valid:
                continue
            score = rules.build_score(result.transaction, rules.all_hits(result.transaction, 1))
            if number == dead_letter_row:
                assert (score.risk_score, score.risk_level) == (0, "LOW")
            else:
                assert (score.risk_score, score.risk_level) == expected[number], f"row {number}"
        assert sorted(expected) == [1, 2, 3, 4, 5, 9, 10, 11, 12]

    def test_exactly_one_row_is_high_risk_so_exactly_one_alert_email_is_sent(self, demo):
        levels = [rules.build_score(r.transaction, rules.all_hits(r.transaction, 1)).risk_level
                  for r in demo["results"] if r.is_valid]
        assert levels.count("HIGH") == 1 and levels.count("MEDIUM") == 1
        assert demo["rows"][4]["transaction_id"] == demo["high_id"]

    def test_dead_letter_row_is_valid_but_for_an_account_that_cannot_exist_yet(self, demo):
        row = demo["rows"][7]
        assert row["transaction_id"] == demo["dead_letter_id"] and row["account_id"] == demo["new_account"]
        assert ACCOUNT_ID_PATTERN.match(demo["new_account"])
        assert demo["new_account"] not in (ROOT / "sql" / "seed.sql").read_text()
        assert onboard_account.ACCOUNT_PATTERN.match(demo["new_account"])

    def test_ids_are_valid_and_unique_within_a_file(self, demo):
        ids = [row["transaction_id"] for row in demo["rows"]]
        assert len(set(ids)) == 12 and all(TRANSACTION_ID_PATTERN.match(i) for i in ids)

    def test_a_second_run_never_reuses_an_id_or_the_account(self, demo):
        _, later_text, _, _, later_account = make_demo_file.build(NOW + timedelta(seconds=1))
        later_ids = {row["transaction_id"] for row in csv.DictReader(io.StringIO(later_text))}
        assert not later_ids & {row["transaction_id"] for row in demo["rows"]}
        assert later_account != demo["new_account"]

    def test_no_row_is_dated_in_the_future(self, demo):
        for row in demo["rows"]:
            assert datetime.fromisoformat(row["tx_date"]) < NOW

    def test_file_name_is_accepted_by_the_upload_route(self, demo):
        from fintrust.query import FILENAME_PATTERN
        assert FILENAME_PATTERN.match(demo["name"])

    def test_file_fits_the_dashboard_progress_table(self, demo):
        limit = int(re.search(r"const TRACK_LIMIT = (\d+);", (ROOT / "frontend" / "app.js").read_text()).group(1))
        assert len(demo["rows"]) <= limit


class TestOnboardAccount:
    @pytest.mark.parametrize("bad", ["", "1001", "ACC-12", "acc-1001", "ACC-1001; DROP TABLE accounts", "ACC-10x1"])
    def test_malformed_account_id_is_refused_before_touching_the_database(self, bad):
        connection = MagicMock()
        with pytest.raises(ValueError):
            onboard_account.onboard(bad, connection)
        connection.cursor.assert_not_called()

    def test_values_are_passed_as_parameters_never_built_into_the_sql(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None           # the account does not exist yet
        cursor.rowcount = 1
        assert onboard_account.onboard("ACC-9001", connection) is True
        statements = [call.args for call in cursor.execute.call_args_list]
        assert len(statements) == 3
        for sql, _values in statements:
            assert "ACC-9001" not in sql and "%s" in sql
        assert statements[2][1] == ("ACC-9001", "CUST-9001")

    def test_existing_account_reports_nothing_changed_and_inserts_nothing(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (1,)
        assert onboard_account.onboard("ACC-9001", connection) is False
        assert cursor.execute.call_count == 1


class TestDiagrams:
    DIAGRAMS = ROOT / "docs" / "diagrams"

    def test_committed_diagrams_are_what_the_generator_produces(self):
        boxes = build_diagrams.architecture_boxes()
        tables, relationships = build_diagrams.read_erd()
        assert (self.DIAGRAMS / "architecture.svg").read_text() == build_diagrams.architecture_svg(boxes)
        assert (self.DIAGRAMS / "architecture.drawio").read_text() == build_diagrams.architecture_drawio(boxes)
        assert (self.DIAGRAMS / "erd.svg").read_text() == build_diagrams.erd_svg(tables, relationships)
        assert (self.DIAGRAMS / "erd.drawio").read_text() == build_diagrams.erd_drawio(tables, relationships)

    @pytest.mark.parametrize("name", ["architecture.pdf", "erd.pdf"])
    def test_pdf_exports_exist(self, name):
        assert (self.DIAGRAMS / name).read_bytes().startswith(b"%PDF")

    def test_architecture_shows_every_service_the_rubric_names_and_the_security_boundaries(self):
        picture = (self.DIAGRAMS / "architecture.svg").read_text()
        for required in ("S3 raw bucket", "ingest Lambda", "SQS FIFO queue", "score Lambda", "RDS PostgreSQL",
                         "DynamoDB", "API Gateway", "Amazon SES", "alert Lambda", "query Lambda", "report Lambda",
                         "EventBridge", "dead-letter queue", "CloudWatch", "IAM", "KMS", "VPC:", "No internet gateway",
                         "no public endpoint", "CloudFront"):
            assert required in picture, required
        assert picture.count("Lambda</text>") >= 5

    def test_every_arrow_joins_two_boxes_that_exist(self):
        boxes = build_diagrams.architecture_boxes()
        for source, target, _label, _waypoints, where in build_diagrams.ARCHITECTURE_ARROWS:
            assert source in boxes and target in boxes and 0 <= where <= 1

    def test_no_two_boxes_overlap(self):
        placed = list(build_diagrams.architecture_boxes().items())
        for index, (first, (x1, y1, w1, h1, *_)) in enumerate(placed):
            for second, (x2, y2, w2, h2, *_) in placed[index + 1:]:
                apart = x1 + w1 <= x2 or x2 + w2 <= x1 or y1 + h1 <= y2 or y2 + h2 <= y1
                assert apart, f"{first} overlaps {second}"

    def test_erd_diagram_has_every_table_column_and_key_from_the_tested_erd(self):
        tables, relationships = build_diagrams.read_erd()
        assert sorted(tables) == ["ACCOUNTS", "ALERTS", "CUSTOMERS", "RISK_EVENTS", "TRANSACTIONS"]
        assert len(relationships) == 5
        picture = (self.DIAGRAMS / "erd.svg").read_text()
        for name, columns in tables.items():
            assert f">{name.lower()}</text>" in picture
            for column in columns:
                assert f">{column[1]}</text>" in picture, f"{name}.{column[1]}"
        keys = sum(len(column) > 2 for columns in tables.values() for column in columns)
        assert picture.count(">PK</text>") + picture.count(">FK</text>") == keys == 10
