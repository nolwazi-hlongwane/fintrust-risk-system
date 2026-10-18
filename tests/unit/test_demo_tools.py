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

    def test_the_high_row_is_thandis_r68_000_as_the_presentation_script_tells_it(self, demo):
        row = demo["rows"][4]
        assert (row["account_id"], row["amount"]) == ("ACC-1002", "68000.00") and row["country_code"] != "ZA"
        seed = (ROOT / "sql" / "seed.sql").read_text()
        assert "('ACC-1002', 'CUST-0001'" in seed and "('CUST-0001', 'Thandiwe Mokoena'" in seed

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


class TestOnboardedCustomerDetails:
    def test_fictional_details_are_stable_and_can_never_reach_a_real_person(self):
        name, email, phone = onboard_account.customer_details("9001")
        assert onboard_account.customer_details("9001") == (name, email, phone)   # same every rehearsal
        assert email.endswith("@example.com") and " 555 " in phone                # reserved for fiction
        assert len(name) <= 120 and len(email) <= 254 and len(phone) <= 20        # the column limits
        assert "demo" not in (name + email).lower()


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


class TestReadyCheck:
    """scripts/ready_check.py is run minutes before presenting, so it must never change anything."""

    SOURCE = (ROOT / "scripts" / "ready_check.py").read_text()
    CHANGING = ("put_", "create_", "delete_", "start_", "stop_", "send_", "invoke", "update_", "modify_",
                "receive_", "purge_", "change_", "admin_", "initiate_auth", "respond_to_auth", "sign_in", "set_")

    def called_names(self):
        tree = ast.parse(self.SOURCE)
        return {node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                for node in ast.walk(tree) if isinstance(node, ast.Call)}

    def test_no_call_that_changes_anything(self):
        offending = {name for name in self.called_names() if name.startswith(self.CHANGING)}
        assert not offending

    def test_every_aws_call_only_reads(self):
        clients = {"rds", "ec2", "lambda_client", "sqs", "cloudwatch", "ses", "cognito", "s3", "sts"}
        aws_calls = {node.func.attr for node in ast.walk(ast.parse(self.SOURCE))
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and isinstance(node.func.value, ast.Name) and node.func.value.id in clients}
        assert aws_calls and all(name.startswith(("get_", "describe_", "list_")) for name in aws_calls)

    def test_it_checks_every_step_the_demonstration_depends_on(self):
        for needed in ("rds_start.sh", "db_tunnel.sh", "transactions-dlq.fifo", "compliance-officer",
                       "monthly/", "status == 401", "alert_sender", "-operations", "CONFIRMED",
                       "FORCE_CHANGE_PASSWORD"):
            assert needed in self.SOURCE, needed

    def test_ready_means_the_api_reaches_the_database(self):
        # RDS can say "available" while the query Lambda still cannot log in (found 18 October).
        assert "}/health\"" in self.SOURCE
        assert "status == 200" in self.SOURCE and "HEALTH_ATTEMPTS" in self.SOURCE
        assert "Authorization" not in self.SOURCE      # still no sign-in


class TestEndToEndAuthentication:
    """Since Week 15 Day 2 API Gateway checks the sign-in before the API key."""

    def test_each_refusal_expects_the_status_api_gateway_really_gives(self):
        assert 'call(url, "GET", "/transactions", None)' in E2E_SOURCE
        assert "no sign-in is refused (401)\", status == 401" in E2E_SOURCE
        assert "Credentials(None, key.id_token)" in E2E_SOURCE
        assert "usage plan (403)\", status == 403" in E2E_SOURCE
        assert 'without the API key is refused", status == 403' not in E2E_SOURCE   # the pre-sign-in check


class TestSlides:
    """docs/presentation/slides.html must show what the system and the demo file actually contain."""

    slides = (ROOT / "docs" / "presentation" / "slides.html").read_text(encoding="utf-8")

    def test_every_diagram_on_a_slide_exists_in_the_repository(self):
        sources = re.findall(r'src="\.\./diagrams/([a-z-]+\.svg)"', self.slides)
        assert sorted(sources) == ["architecture.svg", "erd.svg", "pipeline-sequence.svg"]
        assert all((ROOT / "docs" / "diagrams" / name).is_file() for name in sources)

    def test_the_sms_slide_shows_the_demo_files_high_payment(self):
        amount = f"R{int(float(make_demo_file.HIGH_AMOUNT)):,}".replace(",", " ") + ".00"
        assert amount == "R68 000.00" and amount in self.slides

    def test_the_spend_on_the_slide_is_the_one_in_the_runbook(self):
        total = re.search(r'id="receipt-total"><span>TOTAL SPENT</span><b>(R\d+)</b>', self.slides).group(1)
        assert f"say {total}" in (ROOT / "docs" / "demo-runbook.md").read_text(encoding="utf-8")

    def test_the_architecture_walk_has_the_diagrams_nine_numbered_steps(self):
        assert re.findall(r'n: "(\d)"', self.slides) == [str(n) for n in range(1, 10)]

    def test_the_slides_use_the_dashboards_colours(self):
        css = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
        for token in ("ink", "paper", "brass", "story-accent", "muted", "line"):
            value = re.search(rf"--{token}:\s*(#[0-9a-f]{{6}})", css).group(1)
            assert f"--{token}: {value};" in self.slides, token

    def test_the_slides_never_build_html_from_text(self):
        assert "innerHTML" not in self.slides and "<script src=" not in self.slides
