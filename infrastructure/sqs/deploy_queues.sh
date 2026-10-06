#!/usr/bin/env bash
# Week 13 Day 3: the queues.
#
#   fintrust-capstone-transactions.fifo       ingest Lambda -> score Lambda
#       FIFO: messages with the same MessageGroupId (the account id) are
#       delivered in order, one at a time. The velocity rule depends on that.
#       Different accounts are processed in parallel.
#
#   fintrust-capstone-transactions-dlq.fifo   dead-letter queue (DLQ)
#       After a message has been received 3 times without being deleted (three
#       failed scoring attempts), SQS moves it here instead of retrying forever.
#       It is kept for 14 days so the cause can be fixed and the message
#       "redriven" back to the main queue. Nothing is lost.
#       A FIFO queue's DLQ must also be FIFO.
#
#   fintrust-capstone-ingest-dlq              standard queue
#       Receives the S3 event if the ingest Lambda itself fails after Lambda's
#       own retries, so a failed FILE is not lost either.
#
# All three are encrypted with the project KMS key.
# Safe to run more than once.
# Usage:  ./infrastructure/sqs/deploy_queues.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

if [[ -z "$(kms_key_arn)" ]]; then
  echo "ERROR: KMS key not found. Run ./infrastructure/kms/deploy_key.sh first." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Create the queue if missing, then (re)apply its attributes.
ensure_queue() {  # ensure_queue <name> <attributes-json-file> [create-only-attributes-json-file]
  set -e   # bash turns "stop on error" off inside $(...), so switch it back on here
  local name="$1" attributes="$2" url
  url="$(queue_url "$name")"
  if [[ -z "$url" ]]; then
    url="$(aws sqs create-queue --queue-name "$name" --attributes "file://${3:-$attributes}" \
        --tags Project=fintrust-capstone --query QueueUrl --output text)"
    echo "Created queue  $name" >&2
  else
    echo "Queue exists   $name" >&2
  fi
  aws sqs set-queue-attributes --queue-url "$url" --attributes "file://$attributes"
  echo "$url"
}

# ---------- 1. dead-letter queue for failed scoring (FIFO) ----------
cat > "$WORK/dlq.json" <<JSON
{
  "MessageRetentionPeriod": "1209600",
  "KmsMasterKeyId": "${KEY_ALIAS}",
  "KmsDataKeyReusePeriodSeconds": "300"
}
JSON
# FifoQueue can only be set when the queue is created, never changed afterwards.
cat > "$WORK/dlq-create.json" <<JSON
{
  "FifoQueue": "true",
  "MessageRetentionPeriod": "1209600",
  "KmsMasterKeyId": "${KEY_ALIAS}",
  "KmsDataKeyReusePeriodSeconds": "300"
}
JSON
DLQ_URL="$(ensure_queue "$DLQ_NAME" "$WORK/dlq.json" "$WORK/dlq-create.json")"

# ---------- 2. main transaction queue (FIFO) ----------
# VisibilityTimeout 60s: while the score Lambda works on a message it is hidden
#   from other consumers. It must be longer than the Lambda timeout (30s), or SQS
#   would hand the message to a second worker mid-processing. AWS suggests up to
#   six times the timeout; twice is used here so a message that keeps failing
#   reaches the DLQ in about 3 minutes (3 x 60s) instead of 9.
# RedrivePolicy: after 3 failed receives, move the message to the DLQ.
# ContentBasedDeduplication false: ingest sends the transaction id as the
#   deduplication id, so the same transaction sent twice within 5 minutes is
#   accepted only once.
cat > "$WORK/main.json" <<JSON
{
  "VisibilityTimeout": "60",
  "MessageRetentionPeriod": "345600",
  "ContentBasedDeduplication": "false",
  "KmsMasterKeyId": "${KEY_ALIAS}",
  "KmsDataKeyReusePeriodSeconds": "300",
  "RedrivePolicy": "{\\"deadLetterTargetArn\\":\\"$(queue_arn "$DLQ_NAME")\\",\\"maxReceiveCount\\":\\"3\\"}"
}
JSON
cat > "$WORK/main-create.json" <<JSON
{
  "FifoQueue": "true",
  "VisibilityTimeout": "60",
  "MessageRetentionPeriod": "345600",
  "ContentBasedDeduplication": "false",
  "KmsMasterKeyId": "${KEY_ALIAS}",
  "KmsDataKeyReusePeriodSeconds": "300",
  "RedrivePolicy": "{\\"deadLetterTargetArn\\":\\"$(queue_arn "$DLQ_NAME")\\",\\"maxReceiveCount\\":\\"3\\"}"
}
JSON
QUEUE_URL="$(ensure_queue "$QUEUE_NAME" "$WORK/main.json" "$WORK/main-create.json")"

# Only the main queue may use this DLQ.
cat > "$WORK/dlq-allow.json" <<JSON
{
  "RedriveAllowPolicy": "{\\"redrivePermission\\":\\"byQueue\\",\\"sourceQueueArns\\":[\\"$(queue_arn "$QUEUE_NAME")\\"]}"
}
JSON
aws sqs set-queue-attributes --queue-url "$DLQ_URL" --attributes "file://$WORK/dlq-allow.json"

# ---------- 3. dead-letter queue for failed ingest invocations (standard) ----------
INGEST_DLQ_URL="$(ensure_queue "$INGEST_DLQ_NAME" "$WORK/dlq.json")"

echo "Saving settings:"
put_param queue_url "$QUEUE_URL"

echo
echo "Queues ready:"
echo "  Main       $QUEUE_URL"
echo "  DLQ        $DLQ_URL"
echo "  Ingest DLQ $INGEST_DLQ_URL"
aws sqs get-queue-attributes --queue-url "$QUEUE_URL" \
    --attribute-names FifoQueue VisibilityTimeout RedrivePolicy KmsMasterKeyId \
    --query Attributes --output table
