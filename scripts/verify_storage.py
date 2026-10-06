"""Verify the Week 13 Day 3 security and storage controls on the deployed account.

    python scripts/verify_storage.py

This does not trust the deployment scripts: it asks AWS what is actually
configured, and it PROVES two behaviours by doing them:

  * an object uploaded with no encryption settings comes out encrypted with the
    project KMS key (default encryption works)
  * a message that fails three times really is moved to the dead-letter queue

Test objects and messages are removed afterwards. Exit code 0 means every
check passed.
"""

import json
import os
import sys
import time
import uuid

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "eu-north-1")
PREFIX = "fintrust-capstone"
BUCKETS = ("raw", "processed", "quarantine", "reports")
LONG_RETENTION_DAYS = 2555     # 7 years
GLACIER_AFTER_DAYS = 1095      # 3 years
QUARANTINE_RETENTION_DAYS = 90

session = boto3.session.Session(region_name=REGION)
s3 = session.client("s3")
sqs = session.client("sqs")
kms = session.client("kms")
account_id = session.client("sts").get_caller_identity()["Account"]

results = []


def check(name, passed, detail=""):
    results.append((name, bool(passed), detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def bucket_name(tier):
    return f"{PREFIX}-{tier}-{account_id}"


def verify_key():
    print("\nKMS key")
    key = kms.describe_key(KeyId=f"alias/{PREFIX}")["KeyMetadata"]
    check("customer-managed key exists and is enabled",
          key["KeyManager"] == "CUSTOMER" and key["KeyState"] == "Enabled")
    check("automatic rotation is on", kms.get_key_rotation_status(KeyId=key["Arn"])["KeyRotationEnabled"])
    return key["Arn"]


def verify_bucket(tier, key_arn):
    bucket = bucket_name(tier)
    print(f"\nBucket {bucket}")

    block = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
    check("all public access is blocked", all(block.values()))

    rule = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]["Rules"][0]
    default = rule["ApplyServerSideEncryptionByDefault"]
    check("default encryption uses the project KMS key",
          default["SSEAlgorithm"] == "aws:kms" and default["KMSMasterKeyID"] == key_arn)
    check("bucket key is enabled", rule.get("BucketKeyEnabled") is True)

    check("versioning is enabled", s3.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled")

    try:
        statements = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])["Statement"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchBucketPolicy":
            raise
        statements = []
    tls = [
        s for s in statements
        if s["Effect"] == "Deny"
        and s.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false"
    ]
    check("bucket policy denies requests without TLS", len(tls) == 1)

    lifecycle = s3.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"][0]
    expires = lifecycle["Expiration"]["Days"]
    if tier == "quarantine":
        check("rejected records are deleted after 90 days",
              expires == QUARANTINE_RETENTION_DAYS, f"{expires} days")
    else:
        glacier = [t["Days"] for t in lifecycle.get("Transitions", []) if t["StorageClass"] == "GLACIER"]
        check("moves to Glacier after 3 years", glacier == [GLACIER_AFTER_DAYS], f"{glacier} days")
        check("deleted after 7 years", expires == LONG_RETENTION_DAYS, f"{expires} days")

    # Prove default encryption by uploading with NO encryption settings.
    key = f"verify/{uuid.uuid4()}.txt"
    s3.put_object(Bucket=bucket, Key=key, Body=b"fintrust storage verification")
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        check("an uploaded object is encrypted with the project key",
              head.get("ServerSideEncryption") == "aws:kms" and head.get("SSEKMSKeyId") == key_arn)
    finally:
        for version in s3.list_object_versions(Bucket=bucket, Prefix=key).get("Versions", []):
            s3.delete_object(Bucket=bucket, Key=key, VersionId=version["VersionId"])
        for marker in s3.list_object_versions(Bucket=bucket, Prefix=key).get("DeleteMarkers", []):
            s3.delete_object(Bucket=bucket, Key=key, VersionId=marker["VersionId"])


def receive(queue_url, visibility_timeout):
    response = sqs.receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=2,
        VisibilityTimeout=visibility_timeout, AttributeNames=["ApproximateReceiveCount"],
    )
    return response.get("Messages", [])


def verify_queues(key_arn):
    print("\nQueues")
    main_url = sqs.get_queue_url(QueueName=f"{PREFIX}-transactions.fifo")["QueueUrl"]
    dlq_url = sqs.get_queue_url(QueueName=f"{PREFIX}-transactions-dlq.fifo")["QueueUrl"]
    ingest_dlq_url = sqs.get_queue_url(QueueName=f"{PREFIX}-ingest-dlq")["QueueUrl"]

    main = sqs.get_queue_attributes(QueueUrl=main_url, AttributeNames=["All"])["Attributes"]
    dlq = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["All"])["Attributes"]
    ingest_dlq = sqs.get_queue_attributes(QueueUrl=ingest_dlq_url, AttributeNames=["All"])["Attributes"]

    check("transaction queue is FIFO", main.get("FifoQueue") == "true")
    check("dead-letter queue is FIFO", dlq.get("FifoQueue") == "true")
    redrive = json.loads(main.get("RedrivePolicy", "{}"))
    check("failed messages go to the dead-letter queue after 3 receives",
          redrive.get("deadLetterTargetArn") == dlq["QueueArn"]
          and int(redrive.get("maxReceiveCount", 0)) == 3)
    check("dead-letter queue keeps messages for 14 days", dlq.get("MessageRetentionPeriod") == "1209600")
    queues = (("transaction queue", main), ("dead-letter queue", dlq), ("ingest DLQ", ingest_dlq))
    for name, attributes in queues:
        check(f"{name} is encrypted with the project key",
              attributes.get("KmsMasterKeyId") in (f"alias/{PREFIX}", key_arn))

    # Prove the dead-letter behaviour: receive one message three times without
    # deleting it (three "failed attempts"); the next receive must find nothing,
    # and the message must be waiting in the DLQ.
    marker = f"VERIFY-{uuid.uuid4()}"
    sqs.send_message(
        QueueUrl=main_url, MessageBody=json.dumps({"verification": marker}),
        MessageGroupId="VERIFY-STORAGE", MessageDeduplicationId=marker,
    )
    attempts = 0
    for _ in range(3):
        if receive(main_url, visibility_timeout=0):
            attempts += 1
        time.sleep(1)
    check("message was delivered three times", attempts == 3, f"{attempts} deliveries")
    check("fourth delivery is refused", receive(main_url, visibility_timeout=0) == [])

    found = None
    for _ in range(5):
        for message in receive(dlq_url, visibility_timeout=30):
            if marker in message["Body"]:
                found = message
            sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=message["ReceiptHandle"])
        if found:
            break
        time.sleep(2)
    check("message arrived in the dead-letter queue (nothing lost)", found is not None)


def main():
    print(f"Verifying storage controls in account {account_id}, region {REGION}")
    try:
        key_arn = verify_key()
        for tier in BUCKETS:
            verify_bucket(tier, key_arn)
        verify_queues(key_arn)
    except ClientError as error:
        print(f"\nStopped: AWS returned an error: {error}", file=sys.stderr)
        return 1

    failed = [name for name, passed, _detail in results if not passed]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
