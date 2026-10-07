"""Guard rails for the monitoring configuration files (Week 14 Day 2).

A wrong metric name in an alarm or on the dashboard does not cause an error in
AWS: the alarm simply never fires and the chart stays empty. These tests catch
that before deployment, and keep the project inside the Free Tier limits.
"""

import json
import re
from pathlib import Path

import pytest

from fintrust import metrics

ROOT = Path(__file__).resolve().parents[2]
MONITORING = ROOT / "infrastructure" / "monitoring"
NAMESPACE = "FinTrust/ETL"
FUNCTIONS = {"ingest", "score", "alert", "query"}


def load(name):
    return json.loads((MONITORING / name).read_text())


def custom_metric_names(node):
    """Every metric name used with the FinTrust/ETL namespace anywhere in a JSON document."""
    names = set()
    if isinstance(node, dict):
        if node.get("Namespace") == NAMESPACE and "MetricName" in node:
            names.add(node["MetricName"])
        for value in node.values():
            names |= custom_metric_names(value)
    elif isinstance(node, list):
        if len(node) >= 2 and node[0] == NAMESPACE and isinstance(node[1], str):
            names.add(node[1])
        for value in node:
            names |= custom_metric_names(value)
    return names


class TestAlarmDefinitions:
    def test_error_rate_uses_metrics_the_score_lambda_publishes(self):
        definition = load("alarm-scoring-error-rate.json")
        assert custom_metric_names(definition) == {"ScoringFailures", "TransactionsScored"}
        assert custom_metric_names(definition) <= set(metrics.METRICS)

    def test_error_rate_returns_exactly_one_series_and_cannot_divide_by_zero(self):
        definition = load("alarm-scoring-error-rate.json")
        returned = [entry for entry in definition if entry["ReturnData"]]
        assert len(returned) == 1
        assert returned[0]["Expression"] == "IF(failures + scored > 0, 100 * failures / (failures + scored), 0)"
        assert {entry["Id"] for entry in definition} == {"failures", "scored", "error_rate"}

    def test_custom_metrics_in_alarms_carry_the_environment_dimension(self):
        for entry in load("alarm-scoring-error-rate.json"):
            if "MetricStat" in entry:
                assert entry["MetricStat"]["Metric"]["Dimensions"] == [{"Name": "Environment", "Value": "__ENV__"}]
                assert entry["MetricStat"]["Period"] == 300 and entry["MetricStat"]["Stat"] == "Sum"

    def test_lambda_errors_alarm_covers_every_deployed_function(self):
        definition = load("alarm-lambda-errors.json")
        watched = {entry["MetricStat"]["Metric"]["Dimensions"][0]["Value"] for entry in definition
                   if "MetricStat" in entry}
        assert watched == {f"__PREFIX__-{name}" for name in FUNCTIONS}
        expression = next(entry["Expression"] for entry in definition if entry["ReturnData"])
        # FILL(..., 0): a function that was not invoked has no data, which must count as zero errors
        assert all(f"FILL({name}, 0)" in expression for name in FUNCTIONS)

    def test_deploy_script_creates_seven_alarms_inside_the_free_tier(self):
        script = (MONITORING / "deploy_monitoring.sh").read_text()
        own = len(re.findall(r"put-metric-alarm --alarm-name", script)) + len(re.findall(r"^dlq_alarm ", script, re.M)) - 1
        assert own == 7
        assert own + 1 <= 10          # + the billing alarm

    def test_every_alarm_notifies_and_treats_missing_data_as_healthy(self):
        script = (MONITORING / "deploy_monitoring.sh").read_text()
        assert '--alarm-actions "$TOPIC_ARN" --ok-actions "$TOPIC_ARN" --treat-missing-data notBreaching' in script
        assert script.count('"${COMMON[@]}"') == 5       # 4 direct alarms + the shared dead-letter alarm function


class TestDashboard:
    def test_widgets_fit_the_24_column_grid_without_overlapping(self):
        taken = set()
        for widget in load("dashboard.json")["widgets"]:
            assert widget["x"] + widget["width"] <= 24
            cells = {(x, y) for x in range(widget["x"], widget["x"] + widget["width"])
                     for y in range(widget["y"], widget["y"] + widget["height"])}
            assert not cells & taken, widget["properties"].get("title")
            taken |= cells

    def test_only_published_metrics_are_charted(self):
        used = custom_metric_names(load("dashboard.json"))
        assert used <= set(metrics.METRICS)
        assert used == set(metrics.METRICS)        # and every metric we pay for is shown

    def test_alarm_widget_lists_the_same_seven_alarms_the_script_creates(self):
        widget = next(w for w in load("dashboard.json")["widgets"] if w["type"] == "alarm")
        names = {arn.rsplit(":alarm:__PREFIX__-", 1)[1] for arn in widget["properties"]["alarms"]}
        script = (MONITORING / "deploy_monitoring.sh").read_text()
        assert len(names) == 7
        for name in names:
            assert f"-{name}" in script or f"dlq_alarm {name}" in script

    def test_all_three_dead_letter_queues_are_charted(self):
        text = json.dumps(load("dashboard.json"))
        for queue in ("transactions-dlq.fifo", "ingest-dlq", "alert-dlq"):
            assert f"__PREFIX__-{queue}" in text

    def test_no_account_specific_values_are_committed(self):
        text = (MONITORING / "dashboard.json").read_text()
        assert not re.search(r"\b\d{12}\b", text)
        assert "eu-north-1" not in text


class TestSavedQueries:
    def test_names_are_unique_and_functions_exist(self):
        queries = load("queries.json")
        assert len({query["name"] for query in queries}) == len(queries) == 9
        for query in queries:
            assert set(query["functions"]) <= FUNCTIONS and query["functions"]

    def test_the_brief_failed_ingestion_query_is_present(self):
        query = next(q for q in load("queries.json") if "Failed ingestion" in q["name"])
        assert query["functions"] == ["ingest"]
        assert 'level = "ERROR"' in query["query"]

    @pytest.mark.parametrize("query", load("queries.json"), ids=lambda q: q["name"][:1])
    def test_every_saved_query_is_documented_word_for_word(self, query):
        document = (ROOT / "docs" / "logs-insights-queries.md").read_text()
        assert f"### {query['name']}" in document
        assert query["query"] in document

    def test_queries_filter_on_messages_the_code_really_logs(self):
        source = "".join(path.read_text() for path in (ROOT / "fintrust").glob("*.py"))
        for query in load("queries.json"):
            for message in re.findall(r'"([a-z_]+)"', query["query"]):
                assert f'"{message}"' in source, f"{query['name']}: nothing logs '{message}'"


class TestKeyPolicy:
    def test_only_cloudwatch_is_added_to_the_default_policy(self):
        policy = json.loads((ROOT / "infrastructure" / "kms" / "key-policy.json").read_text())
        account, service = policy["Statement"]
        assert account["Principal"] == {"AWS": "arn:aws:iam::__ACCOUNT_ID__:root"}
        assert service["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
        assert service["Action"] == ["kms:Decrypt", "kms:GenerateDataKey"]
        assert len(policy["Statement"]) == 2
