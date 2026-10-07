#!/usr/bin/env bash
# Move every message in the scoring dead-letter queue back to the scoring queue,
# so it is scored again. Run this AFTER fixing whatever made the messages fail
# (for example, after the missing account has been onboarded).
#
# This is the "recover" half of the dead-letter queue: nothing was lost while
# the messages waited (they are kept for 14 days), and nothing is retyped or
# re-uploaded to recover them.
#
# Usage:  ./scripts/redrive_dlq.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../infrastructure/common.sh"

DLQ_URL="$(queue_url "$DLQ_NAME")"
if [[ -z "$DLQ_URL" ]]; then
  echo "ERROR: queue $DLQ_NAME not found. Run ./infrastructure/sqs/deploy_queues.sh first." >&2
  exit 1
fi

waiting() {
  aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
      --attribute-names ApproximateNumberOfMessages \
      --query Attributes.ApproximateNumberOfMessages --output text
}

COUNT="$(waiting)"
echo "Messages in ${DLQ_NAME}: ${COUNT}"
if [[ "$COUNT" == "0" ]]; then
  echo "Nothing to redrive."
  exit 0
fi

# With no destination given, SQS sends each message back to the queue it came from.
aws sqs start-message-move-task --source-arn "$(queue_arn "$DLQ_NAME")" >/dev/null
echo "Redrive started: moving ${COUNT} message(s) back to ${QUEUE_NAME}"

for _ in $(seq 1 30); do
  sleep 2
  if [[ "$(waiting)" == "0" ]]; then
    echo "Done. The dead-letter queue is empty; the score Lambda is processing the message(s) now."
    exit 0
  fi
done
echo "Still moving. Check again with:  aws sqs list-message-move-tasks --source-arn $(queue_arn "$DLQ_NAME")"
