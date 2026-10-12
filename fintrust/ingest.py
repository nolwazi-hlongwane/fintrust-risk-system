"""Ingest Lambda (ETL): triggered by S3 when a CSV file lands in the raw bucket.

For each file:

    1. EXTRACT    read the CSV from the raw bucket
    2. VALIDATE   check every row (fintrust/validators.py)
    3. QUARANTINE write rejected rows, with every reason, to the quarantine bucket
    4. PRE-SCORE  apply the risk rules that need only the row itself
    5. LOAD       write the clean rows to the processed bucket, then send one
                  message per transaction to the SQS FIFO queue for scoring
    6. RECORD     write each transaction's journey steps to DynamoDB

This Lambda runs OUTSIDE the VPC: it needs SQS, which has no free VPC endpoint,
and it never touches the database. The scoring Lambda (inside the VPC) reads
the queue and writes the ledger.

Nothing is silently dropped. Every row of every file ends up in exactly one of:
the queue (valid), the quarantine bucket (invalid), or, if this function itself
fails after Lambda's retries, the whole S3 event goes to the ingest dead-letter
queue. The counts are logged so they can be reconciled against the file.

Safe to run twice on the same file (Lambda retries a failed invocation):
output objects have fixed names so they are overwritten, and SQS ignores a
repeated MessageDeduplicationId, which is the transaction id.
"""

import csv
import io
import json
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from fintrust import config, dynamo, metrics, rules, validators
from fintrust.log import get_logger

logger = get_logger("ingest")

MAX_FILE_BYTES = 10 * 1024 * 1024   # 10 MB
MAX_ROWS = 5000                     # keeps one invocation well inside its timeout
SQS_BATCH_SIZE = 10                 # the most SQS accepts in one call

PROCESSED_COLUMNS = (
    "transaction_id", "account_id", "tx_date", "amount", "currency", "tx_type", "channel",
    "merchant_name", "country_code", "description", "pre_score", "pre_flags",
)

# Module level: created once per Lambda container and reused on warm invocations.
_s3 = boto3.client("s3", region_name=config.region())
_sqs = boto3.client("sqs", region_name=config.region())


class IngestError(Exception):
    """The file could not be fully processed. Lambda will retry the invocation."""


class FileRejected(Exception):
    """The whole file is unusable (not a problem with individual rows)."""


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------
def _stem(key):
    """uploads/2026-10-06/batch.csv -> uploads/2026-10-06/batch"""
    return key[:-4] if key.lower().endswith(".csv") else key


def _to_csv(columns, rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _put(bucket, key, body, content_type):
    # No encryption arguments: the bucket's default encryption (the project KMS
    # key) is applied automatically by S3.
    _s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)


def _record(transaction, stage, detail=None):
    """Write one journey step. The journey is an audit trail: if DynamoDB is
    unavailable the transaction must still be processed, so a failure here is
    logged and counted, not raised."""
    try:
        dynamo.put_event(
            transaction.account_id, transaction.tx_timestamp, transaction.transaction_id, stage, detail
        )
        return True
    except dynamo.DynamoError:
        return False


# ----------------------------------------------------------------------------
# steps
# ----------------------------------------------------------------------------
def read_rows(bucket, key):
    """EXTRACT. Returns (header, rows, ignored_columns). Raises FileRejected."""
    response = _s3.get_object(Bucket=bucket, Key=key)
    if response["ContentLength"] > MAX_FILE_BYTES:
        raise FileRejected(f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    raw = response["Body"].read()
    if not raw.strip():
        raise FileRejected("file is empty")
    try:
        text = raw.decode("utf-8-sig")   # utf-8-sig also removes Excel's invisible first character
    except UnicodeDecodeError:
        raise FileRejected("file is not UTF-8 text") from None

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        original_header = next(reader)
        data = list(reader)
    except (csv.Error, StopIteration) as error:
        raise FileRejected(f"file is not a readable CSV: {error}") from None

    problems = validators.validate_header(original_header)
    if problems:
        raise FileRejected("; ".join(problems))
    if len(data) > MAX_ROWS:
        raise FileRejected(f"file has {len(data)} rows; the limit is {MAX_ROWS} per file")

    header = validators.normalise_header(original_header)
    rows = []
    for line_number, values in enumerate(data, start=2):   # line 1 is the header
        if not any(value.strip() for value in values):
            continue                                        # skip completely blank lines
        row = dict(zip(header, values, strict=False))
        row["_line"] = line_number
        if len(values) != len(header):
            row["_shape_error"] = f"row has {len(values)} values but the header has {len(header)} columns"
        rows.append(row)
    return header, rows, validators.ignored_columns(original_header)


def validate_rows(rows, source_file):
    """VALIDATE. Returns (valid, rejected, warning_count).

    valid     list of Transaction
    rejected  list of dicts: the original values plus row_number and errors
    """
    valid, rejected, warning_count = [], [], 0
    seen = set()
    for row in rows:
        errors = []
        if "_shape_error" in row:
            errors.append(row["_shape_error"])
        result = validators.validate_row(row, source_file)
        errors.extend(result.errors)
        if result.is_valid and result.transaction.transaction_id in seen:
            errors.append("transaction_id appears more than once in this file")

        if errors:
            rejected.append({**row, "row_number": row["_line"], "errors": "; ".join(errors)})
            continue
        seen.add(result.transaction.transaction_id)
        warning_count += len(result.warnings)
        valid.append(result.transaction)
    return valid, rejected, warning_count


def pre_score(transaction):
    """PRE-SCORE with the rules that need only the row itself."""
    hits = rules.stateless_hits(transaction)
    return {"points": rules.total_points(hits), "flags": [hit.rule for hit in hits]}


def send_to_queue(transactions, pre_scores, source_key):
    """LOAD into SQS. Returns the transactions that were accepted by the queue.

    MessageGroupId = account id          one account's transactions stay in order
    MessageDeduplicationId = transaction id   a repeat within 5 minutes is ignored
    """
    queue_url = config.get("queue_url")
    sent = []
    failed = []
    for start in range(0, len(transactions), SQS_BATCH_SIZE):
        batch = transactions[start:start + SQS_BATCH_SIZE]
        entries = []
        for index, transaction in enumerate(batch):
            message = transaction.to_message()
            message["pre_score"] = pre_scores[transaction.transaction_id]
            message["source_key"] = source_key
            entries.append(
                {
                    "Id": str(index),
                    "MessageBody": json.dumps(message),
                    "MessageGroupId": transaction.account_id,
                    "MessageDeduplicationId": transaction.transaction_id,
                }
            )
        try:
            response = _sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)
        except (ClientError, BotoCoreError) as error:
            logger.error("sqs_batch_failed", error=str(error), batch_size=len(batch))
            failed.extend(batch)
            continue
        accepted = {int(entry["Id"]) for entry in response.get("Successful", [])}
        for index, transaction in enumerate(batch):
            (sent if index in accepted else failed).append(transaction)
        for entry in response.get("Failed", []):
            logger.error(
                "sqs_message_rejected",
                transaction_id=batch[int(entry["Id"])].transaction_id,
                code=entry.get("Code"),
                reason=entry.get("Message"),
            )
    return sent, failed


def process_file(bucket, key, log):
    """Run the whole pipeline for one file. Returns a summary dict."""
    stem = _stem(key)
    quarantine_bucket = config.get("quarantine_bucket")
    summary = {
        "source_key": key, "status": "PROCESSED", "rows_read": 0, "rows_valid": 0,
        "rows_quarantined": 0, "rows_queued": 0, "warnings": 0, "pre_score_flagged": 0,
        "journey_write_failures": 0,
    }

    # ---- 1. extract ----
    try:
        header, rows, ignored = read_rows(bucket, key)
    except FileRejected as rejection:
        # The file itself is unusable. Record why, next to where its rows would
        # have gone, and finish normally: retrying will not fix a bad file.
        reason = {"source_bucket": bucket, "source_key": key, "reason": str(rejection)}
        _put(quarantine_bucket, f"{stem}.file-rejected.json", json.dumps(reason, indent=2).encode(),
             "application/json")
        log.error("file_rejected", reason=str(rejection))
        summary.update(status="FILE_REJECTED", reason=str(rejection))
        return summary

    summary["rows_read"] = len(rows)
    if ignored:
        # POPIA minimality: columns the system does not need are never read.
        log.warning("columns_ignored", columns=ignored)

    # ---- 2. validate ----
    valid, rejected, warning_count = validate_rows(rows, key)
    summary.update(rows_valid=len(valid), rows_quarantined=len(rejected), warnings=warning_count)

    # ---- 3. quarantine ----
    if rejected:
        columns = ["row_number", "errors", *header]
        _put(quarantine_bucket, f"{stem}.rejected.csv", _to_csv(columns, rejected), "text/csv")
        log.warning("rows_quarantined", count=len(rejected), quarantine_key=f"{stem}.rejected.csv")

    if not valid:
        log.info("file_processed", **summary)
        return summary

    # ---- 4. pre-score ----
    pre_scores = {transaction.transaction_id: pre_score(transaction) for transaction in valid}
    summary["pre_score_flagged"] = sum(1 for score in pre_scores.values() if score["flags"])

    # ---- 5. load: processed bucket ----
    clean_rows = []
    for transaction in valid:
        clean = transaction.to_message()
        clean["pre_score"] = pre_scores[transaction.transaction_id]["points"]
        clean["pre_flags"] = "|".join(pre_scores[transaction.transaction_id]["flags"])
        clean_rows.append(clean)
    _put(config.get("processed_bucket"), f"{stem}.clean.csv", _to_csv(PROCESSED_COLUMNS, clean_rows), "text/csv")

    # ---- 6. record, then load: queue ----
    line_of = {row.get("transaction_id", "").strip(): row["_line"] for row in rows}
    journey_failures = 0
    for transaction in valid:
        line = line_of.get(transaction.transaction_id)
        journey_failures += not _record(transaction, "RECEIVED", f"{key} line {line}")
        journey_failures += not _record(transaction, "VALIDATED")

    sent, failed = send_to_queue(valid, pre_scores, key)
    for transaction in sent:
        journey_failures += not _record(transaction, "QUEUED")
    summary["rows_queued"] = len(sent)
    summary["journey_write_failures"] = journey_failures
    if journey_failures:
        log.error("journey_writes_failed", count=journey_failures)

    if failed:
        # Raising makes Lambda retry the whole file. Rows already queued are
        # ignored the second time because of their deduplication id.
        log.error("rows_not_queued", count=len(failed), **summary)
        raise IngestError(f"{len(failed)} of {len(valid)} valid rows could not be queued")

    log.info("file_processed", **summary)
    return summary


def handler(event, context):
    """Lambda entry point. `event` is an S3 notification with one or more records."""
    request_id = getattr(context, "aws_request_id", "local")
    raw_bucket = config.get("raw_bucket")
    summaries = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # S3 URL-encodes the key in the event: "my file.csv" arrives as "my+file.csv".
        key = unquote_plus(record["s3"]["object"]["key"])
        log = logger.bind(request_id=request_id, source_key=key)

        if bucket != raw_bucket:
            log.warning("unexpected_bucket_ignored", bucket=bucket)
            continue
        if not key.lower().endswith(".csv"):
            log.warning("non_csv_object_ignored")
            continue

        log.info("file_received", size_bytes=record["s3"]["object"].get("size"))
        try:
            summary = process_file(bucket, key, log)
        except IngestError:
            raise
        except (ClientError, BotoCoreError) as error:
            log.error("aws_call_failed", error=str(error))
            raise IngestError(f"AWS error while processing {key}") from error
        summaries.append(summary)
        # Published only when the file finished, so a retried file is counted once.
        metrics.emit(
            RowsRead=summary["rows_read"],
            RowsQueued=summary["rows_queued"],
            RowsQuarantined=summary["rows_quarantined"],
            FilesRejected=1 if summary["status"] == "FILE_REJECTED" else 0,
        )

    return {"files": summaries}
