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
    NAMES = ["architecture", "erd", "pipeline-sequence", "sign-in-sequence"]

    @staticmethod
    def labels(drawio_text):
        """Every visible label in a .drawio file, as plain text."""
        from html import unescape
        values = re.findall(r'value="([^"]*)"', drawio_text)
        return " ".join(re.sub(r"<[^>]+>", " ", unescape(unescape(value))) for value in values)

    def test_committed_drawio_files_are_what_the_generator_produces(self):
        assert sorted(build_diagrams.DIAGRAMS) == sorted(f"{name}.drawio" for name in self.NAMES)
        for name, build in build_diagrams.DIAGRAMS.items():
            assert (self.DIAGRAMS / name).read_text(encoding="utf-8") == build(), name

    @pytest.mark.parametrize("name", NAMES)
    def test_every_picture_was_exported_from_the_current_drawio_file(self, name):
        import export_diagrams

        drawio = self.DIAGRAMS / f"{name}.drawio"
        svg = (self.DIAGRAMS / f"{name}.svg").read_text(encoding="utf-8")
        assert svg.startswith(export_diagrams.stamp(drawio.name, export_diagrams.source_hash(drawio))), (
            f"{name}.svg is out of date: run scripts/export_diagrams.py")
        assert (self.DIAGRAMS / f"{name}.pdf").read_bytes().startswith(b"%PDF")

    def test_no_other_files_in_the_diagrams_folder(self):
        expected = {f"{name}.{kind}" for name in self.NAMES for kind in ("drawio", "svg", "pdf")}
        assert {path.name for path in self.DIAGRAMS.iterdir()} == expected

    def test_architecture_shows_every_service_and_the_security_boundaries(self):
        text = self.labels((self.DIAGRAMS / "architecture.drawio").read_text(encoding="utf-8"))
        for required in ("S3 raw bucket", "ingest Lambda", "SQS FIFO queue", "score Lambda", "RDS PostgreSQL",
                         "DynamoDB events", "DynamoDB sessions", "Amazon API Gateway", "Amazon SES", "alert Lambda",
                         "query Lambda", "report Lambda", "EventBridge rule", "EventBridge Scheduler",
                         "FIFO dead-letter queue", "ingest dead-letter queue", "alert dead-letter queue",
                         "Amazon CloudWatch", "AWS X-Ray", "AWS IAM", "AWS KMS", "SSM Parameter Store",
                         "AWS CloudTrail", "Amazon SNS", "Amazon Cognito", "Amazon CloudFront",
                         "S3 quarantine bucket", "S3 processed bucket", "S3 reports bucket", "S3 dashboard bucket",
                         "no internet gateway, no NAT Gateway", "Private subnets", "S3 gateway",
                         "DynamoDB gateway endpoint", "eu-north-1"):
            assert required in text, required

    def test_every_arrow_joins_two_cells_that_exist(self):
        cards = build_diagrams.architecture_cards()
        known = set(cards) | {"s3endpoint", "ddbendpoint"}
        for _id, source, target, _label, kind, exit_at, entry_at, _points in build_diagrams.ARCHITECTURE_ARROWS:
            assert source in known and target in known
            assert kind in build_diagrams.ARROW
            assert all(0 <= value <= 1 for value in (*exit_at, *entry_at))

    def test_no_two_cards_overlap_and_every_card_is_on_the_page(self):
        placed = [(name, x, y) for name, (x, y, *_rest) in build_diagrams.architecture_cards().items()]
        w, h = build_diagrams.CARD_W, build_diagrams.CARD_H
        for index, (first, x1, y1) in enumerate(placed):
            assert x1 >= 0 and y1 >= 0
            for second, x2, y2 in placed[index + 1:]:
                apart = x1 + w <= x2 or x2 + w <= x1 or y1 + h <= y2 or y2 + h <= y1
                assert apart, f"{first} overlaps {second}"

    def test_vpc_lambdas_are_drawn_inside_the_vpc_and_the_others_outside(self):
        cards = build_diagrams.architecture_cards()
        left, top = build_diagrams.VPC_X, build_diagrams.VPC_Y
        right, bottom = left + build_diagrams.VPC_W, top + build_diagrams.VPC_H

        def inside(name):
            x, y = cards[name][:2]
            return left < x and x + build_diagrams.CARD_W < right and top < y and y + build_diagrams.CARD_H < bottom

        for name in ("query", "score", "report", "rds", "tunnel"):
            assert inside(name), name
        for name in ("ingest", "alert", "fifo", "raw", "events", "sessions", "apigw", "cognito"):
            assert not inside(name), name

    def test_every_numbered_step_is_explained_once(self):
        numbers = [number for number, *_ in build_diagrams.STEPS]
        assert sorted(numbers) == list(range(1, len(numbers) + 1))

    def test_erd_diagram_has_every_table_column_and_relationship_from_the_tested_erd(self):
        tables, relationships = build_diagrams.read_erd()
        assert sorted(tables) == ["accounts", "alerts", "customers", "risk_events", "transactions"]
        assert len(relationships) == 5
        text = self.labels((self.DIAGRAMS / "erd.drawio").read_text(encoding="utf-8"))
        for table, columns in tables.items():
            assert f"«table» {table}" in " ".join(text.split()), table
            for column in columns:
                assert build_diagrams.column_text(*column) in text, f"{table}.{column[0]}"
        keys = sum(1 for columns in tables.values() for column in columns if column[2])
        assert keys == 10
        for _parent, one, many, _child, name in relationships:
            assert name in text and one in text and many in text

    @pytest.mark.parametrize(("name", "required"), [
        ("pipeline-sequence", ["POST /upload", "202 Accepted", "ObjectCreated", "SendMessage", "COMMIT", "ROLLBACK",
                               "batchItemFailures", "dead-letter queue", "redrive_dlq.sh", "SendEmail", "alt"]),
        ("sign-in-sequence", ["/oauth2/authorize", "/oauth2/token", "/oauth2/revoke", "POST /session",
                              "DELETE /session", "SESSION_ENDED", "PKCE", "authenticator code", "loop", "opt"]),
    ])
    def test_sequence_diagrams_show_the_real_calls(self, name, required):
        text = self.labels((self.DIAGRAMS / f"{name}.drawio").read_text(encoding="utf-8"))
        for words in required:
            assert words in text, words
