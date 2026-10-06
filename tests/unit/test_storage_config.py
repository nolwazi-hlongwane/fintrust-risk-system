"""Guard rails for the storage configuration files (Week 13 Day 3).

The retention periods here must match the data lifecycle policy in
docs/data-governance.md: Glacier after 3 years, deleted after 7 years,
rejected records deleted after 90 days.
"""

import json
from pathlib import Path

import pytest

S3_DIR = Path(__file__).resolve().parents[2] / "infrastructure" / "s3"
INFRA_DIR = S3_DIR.parent
LONG_LIVED = ["raw", "processed", "reports"]
SEVEN_YEARS = 2555
THREE_YEARS = 1095


def lifecycle(tier):
    rules = json.loads((S3_DIR / f"lifecycle-{tier}.json").read_text())["Rules"]
    assert len(rules) == 1
    return rules[0]


@pytest.mark.parametrize("tier", LONG_LIVED + ["quarantine"])
def test_every_bucket_has_an_enabled_lifecycle_rule_for_all_objects(tier):
    rule = lifecycle(tier)
    assert rule["Status"] == "Enabled"
    assert rule["Filter"] == {}


@pytest.mark.parametrize("tier", LONG_LIVED)
def test_records_are_deleted_after_seven_years(tier):
    assert lifecycle(tier)["Expiration"] == {"Days": SEVEN_YEARS}


@pytest.mark.parametrize("tier", LONG_LIVED)
def test_records_move_to_glacier_after_three_years(tier):
    transitions = {t["StorageClass"]: t["Days"] for t in lifecycle(tier)["Transitions"]}
    assert transitions == {"STANDARD_IA": 30, "GLACIER": THREE_YEARS}


def test_rejected_records_are_deleted_after_90_days():
    rule = lifecycle("quarantine")
    assert rule["Expiration"] == {"Days": 90}
    assert "Transitions" not in rule


@pytest.mark.parametrize("tier", LONG_LIVED + ["quarantine"])
def test_old_versions_and_failed_uploads_are_cleaned_up(tier):
    rule = lifecycle(tier)
    assert rule["NoncurrentVersionExpiration"]["NoncurrentDays"] <= 90
    assert rule["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 7}


def test_bucket_policy_refuses_requests_without_tls():
    policy = json.loads((S3_DIR / "bucket-policy.json").read_text())
    deny = [s for s in policy["Statement"] if s["Sid"] == "DenyAnyRequestNotUsingTLS"]
    assert len(deny) == 1
    assert deny[0]["Effect"] == "Deny"
    assert deny[0]["Principal"] == "*"
    assert deny[0]["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert set(deny[0]["Resource"]) == {"arn:aws:s3:::__BUCKET__", "arn:aws:s3:::__BUCKET__/*"}


def test_bucket_policy_grants_nothing():
    policy = json.loads((S3_DIR / "bucket-policy.json").read_text())
    assert all(statement["Effect"] == "Deny" for statement in policy["Statement"])


def test_bucket_script_applies_every_protection():
    script = (S3_DIR / "deploy_buckets.sh").read_text()
    for required in (
        "put-public-access-block", "BlockPublicAcls=true", "IgnorePublicAcls=true",
        "BlockPublicPolicy=true", "RestrictPublicBuckets=true",
        "put-bucket-encryption", "aws:kms", "BucketKeyEnabled",
        "put-bucket-versioning", "put-bucket-policy", "put-bucket-lifecycle-configuration",
    ):
        assert required in script, f"deploy_buckets.sh no longer applies {required}"


def test_queue_script_settings():
    script = (INFRA_DIR / "sqs" / "deploy_queues.sh").read_text()
    assert '"FifoQueue": "true"' in script
    assert 'maxReceiveCount\\\\":\\\\"3' in script          # dead-letter after 3 failed receives
    assert '"MessageRetentionPeriod": "1209600"' in script          # DLQ keeps messages 14 days
    assert script.count('"VisibilityTimeout": "60"') == 2           # longer than the 30 second Lambda timeout
    assert script.count('"KmsMasterKeyId": "${KEY_ALIAS}"') >= 4    # every queue encrypted
    assert "byQueue" in script                                      # only the main queue may use the DLQ


def test_names_in_common_match_names_in_policies():
    common = (INFRA_DIR / "common.sh").read_text()
    assert 'QUEUE_NAME="${PREFIX}-transactions.fifo"' in common
    assert 'DLQ_NAME="${PREFIX}-transactions-dlq.fifo"' in common
    assert 'INGEST_DLQ_NAME="${PREFIX}-ingest-dlq"' in common
    assert 'echo "${PREFIX}-$1-${ACCOUNT_ID}"' in common
