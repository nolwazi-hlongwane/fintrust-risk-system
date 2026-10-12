#!/usr/bin/env bash
# Week 13 Day 2: the DynamoDB event store.
# Week 15 Day 2: the sign-in session table (the brief's "session tokens").
#
# Table fintrust-capstone-events
#   Partition key  account_id    (S)   all of one account's items live together
#   Sort key       tx_timestamp  (S)   starts with the transaction time, so items
#                                      are stored in time order
#   Index          by_transaction      look up one transaction without the account
#   Capacity       5 read + 10 write units on the table and on the index
#                  (10 read + 20 write of the 25 + 25 units in the Free Tier).
#                  Each transaction writes about 5 small items (journey steps
#                  and its score), so writes need more room than reads.
#   Stream         NEW_IMAGE           used in Week 14 Day 1 to trigger alerts
#   Time-to-live   expires_at          items delete themselves after 12 months
#
# Table fintrust-capstone-sessions (Week 15 Day 2, read by fintrust/sessions.py)
#   Partition key  session_id    (S)   the sign-in's origin_jti from Cognito
#   Capacity       5 read + 5 write units
#   Time-to-live   expires_at          a sign-in record deletes itself after 90 days
#
# Free Tier check: 10 + 5 = 15 of 25 read units, 20 + 5 = 25 of 25 write units.
#
# Safe to run more than once.
# Usage:  ./infrastructure/dynamodb/deploy_table.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

if aws dynamodb describe-table --table-name "$TABLE_NAME" >/dev/null 2>&1; then
  echo "Table $TABLE_NAME exists"
else
  aws dynamodb create-table --table-name "$TABLE_NAME" \
      --attribute-definitions \
          AttributeName=account_id,AttributeType=S \
          AttributeName=tx_timestamp,AttributeType=S \
          AttributeName=transaction_id,AttributeType=S \
      --key-schema \
          AttributeName=account_id,KeyType=HASH \
          AttributeName=tx_timestamp,KeyType=RANGE \
      --billing-mode PROVISIONED \
      --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=10 \
      --global-secondary-indexes '[{
          "IndexName": "by_transaction",
          "KeySchema": [
            {"AttributeName": "transaction_id", "KeyType": "HASH"},
            {"AttributeName": "tx_timestamp", "KeyType": "RANGE"}
          ],
          "Projection": {"ProjectionType": "ALL"},
          "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 10}
        }]' \
      --stream-specification StreamEnabled=true,StreamViewType=NEW_IMAGE \
      --tags Key=Project,Value=fintrust-capstone >/dev/null
  echo "Creating table $TABLE_NAME..."
fi
aws dynamodb wait table-exists --table-name "$TABLE_NAME"

# Bring an existing table up to the capacity above (it was created with 5 write units).
CURRENT_WCU="$(aws dynamodb describe-table --table-name "$TABLE_NAME" \
    --query 'Table.ProvisionedThroughput.WriteCapacityUnits' --output text)"
if [[ "$CURRENT_WCU" != "10" ]]; then
  aws dynamodb update-table --table-name "$TABLE_NAME" \
      --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=10 \
      --global-secondary-index-updates '[{"Update": {"IndexName": "by_transaction",
          "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 10}}}]' >/dev/null
  aws dynamodb wait table-exists --table-name "$TABLE_NAME"
  echo "Raised write capacity to 10 units on the table and its index"
fi

TTL_STATUS="$(aws dynamodb describe-time-to-live --table-name "$TABLE_NAME" \
    --query 'TimeToLiveDescription.TimeToLiveStatus' --output text)"
if [[ "$TTL_STATUS" != "ENABLED" && "$TTL_STATUS" != "ENABLING" ]]; then
  aws dynamodb update-time-to-live --table-name "$TABLE_NAME" \
      --time-to-live-specification "Enabled=true,AttributeName=expires_at" >/dev/null
  echo "Enabled time-to-live on expires_at"
fi

# ---------- the sign-in session table (Week 15 Day 2) ----------
if aws dynamodb describe-table --table-name "$SESSIONS_TABLE" >/dev/null 2>&1; then
  echo "Table $SESSIONS_TABLE exists"
else
  aws dynamodb create-table --table-name "$SESSIONS_TABLE" \
      --attribute-definitions AttributeName=session_id,AttributeType=S \
      --key-schema AttributeName=session_id,KeyType=HASH \
      --billing-mode PROVISIONED \
      --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5 \
      --tags Key=Project,Value=fintrust-capstone >/dev/null
  echo "Creating table $SESSIONS_TABLE..."
fi
aws dynamodb wait table-exists --table-name "$SESSIONS_TABLE"

TTL_STATUS="$(aws dynamodb describe-time-to-live --table-name "$SESSIONS_TABLE" \
    --query 'TimeToLiveDescription.TimeToLiveStatus' --output text)"
if [[ "$TTL_STATUS" != "ENABLED" && "$TTL_STATUS" != "ENABLING" ]]; then
  aws dynamodb update-time-to-live --table-name "$SESSIONS_TABLE" \
      --time-to-live-specification "Enabled=true,AttributeName=expires_at" >/dev/null
  echo "Enabled time-to-live on $SESSIONS_TABLE (expires_at)"
fi

echo "Saving settings:"
put_param dynamo_table "$TABLE_NAME"
put_param sessions_table "$SESSIONS_TABLE"

echo
aws dynamodb describe-table --table-name "$TABLE_NAME" \
    --query 'Table.{Table:TableName,Status:TableStatus,Keys:KeySchema[].AttributeName,Index:GlobalSecondaryIndexes[0].IndexName,Stream:StreamSpecification.StreamViewType}' \
    --output table
aws dynamodb describe-table --table-name "$SESSIONS_TABLE" \
    --query 'Table.{Table:TableName,Status:TableStatus,Keys:KeySchema[].AttributeName,Read:ProvisionedThroughput.ReadCapacityUnits,Write:ProvisionedThroughput.WriteCapacityUnits}' \
    --output table
