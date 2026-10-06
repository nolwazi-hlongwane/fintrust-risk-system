#!/usr/bin/env bash
# Week 13 Day 2: the DynamoDB event store.
#
# Table fintrust-capstone-events
#   Partition key  account_id    (S)   all of one account's items live together
#   Sort key       tx_timestamp  (S)   starts with the transaction time, so items
#                                      are stored in time order
#   Index          by_transaction      look up one transaction without the account
#   Capacity       5 read + 5 write units on the table and on the index
#                  (10 + 10 of the 25 + 25 units in the Free Tier)
#   Stream         NEW_IMAGE           used in Week 14 Day 1 to trigger alerts
#   Time-to-live   expires_at          items delete themselves after 12 months
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
      --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5 \
      --global-secondary-indexes '[{
          "IndexName": "by_transaction",
          "KeySchema": [
            {"AttributeName": "transaction_id", "KeyType": "HASH"},
            {"AttributeName": "tx_timestamp", "KeyType": "RANGE"}
          ],
          "Projection": {"ProjectionType": "ALL"},
          "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}
        }]' \
      --stream-specification StreamEnabled=true,StreamViewType=NEW_IMAGE \
      --tags Key=Project,Value=fintrust-capstone >/dev/null
  echo "Creating table $TABLE_NAME..."
fi
aws dynamodb wait table-exists --table-name "$TABLE_NAME"

TTL_STATUS="$(aws dynamodb describe-time-to-live --table-name "$TABLE_NAME" \
    --query 'TimeToLiveDescription.TimeToLiveStatus' --output text)"
if [[ "$TTL_STATUS" != "ENABLED" && "$TTL_STATUS" != "ENABLING" ]]; then
  aws dynamodb update-time-to-live --table-name "$TABLE_NAME" \
      --time-to-live-specification "Enabled=true,AttributeName=expires_at" >/dev/null
  echo "Enabled time-to-live on expires_at"
fi

echo "Saving setting:"
put_param dynamo_table "$TABLE_NAME"

echo
aws dynamodb describe-table --table-name "$TABLE_NAME" \
    --query 'Table.{Table:TableName,Status:TableStatus,Keys:KeySchema[].AttributeName,Index:GlobalSecondaryIndexes[0].IndexName,Stream:StreamSpecification.StreamViewType}' \
    --output table
