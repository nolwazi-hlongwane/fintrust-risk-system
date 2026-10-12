#!/usr/bin/env bash
# Open a private tunnel from this computer to the RDS database.
#
#   laptop :15432  ->  EC2 Instance Connect Endpoint  ->  tunnel instance  ->  RDS :5432
#
# Leave this running in its own terminal window. While it is open, the database
# answers on localhost port 15432. Press Ctrl+C to close it.
#
# Usage:  ./scripts/db_tunnel.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../infrastructure/common.sh"

INSTANCE_ID="$(tunnel_instance_id)"
if [[ -z "$INSTANCE_ID" ]]; then
  echo "ERROR: tunnel instance not found. Run ./infrastructure/rds/deploy_tunnel.sh first." >&2
  exit 1
fi

DB_HOST="$(aws ssm get-parameter --name "/fintrust/${FINTRUST_ENV}/db_host" \
    --query Parameter.Value --output text)"

STATE="$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].State.Name' --output text)"
if [[ "$STATE" != "running" ]]; then
  echo "Starting the tunnel instance..."
  aws ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null
  aws ec2 wait instance-status-ok --instance-ids "$INSTANCE_ID"
fi
if [[ "$(db_status)" != "available" ]]; then
  echo "ERROR: the database is '$(db_status)', not 'available'. Run ./scripts/rds_start.sh first." >&2
  exit 1
fi

# A key pair used only for this tunnel. The private half never leaves this Mac.
KEY="$HOME/.ssh/fintrust_tunnel"
mkdir -p "$HOME/.ssh"
if [[ ! -f "$KEY" ]]; then
  ssh-keygen -t ed25519 -N "" -f "$KEY" -C "fintrust-db-tunnel" >/dev/null
fi

# Ask AWS to accept this public key on the instance for the next 60 seconds.
# This call is authorised by YOUR IAM identity and recorded in CloudTrail.
aws ec2-instance-connect send-ssh-public-key --instance-id "$INSTANCE_ID" \
    --instance-os-user ec2-user --ssh-public-key "file://${KEY}.pub" >/dev/null

echo "Tunnel open: localhost:${TUNNEL_LOCAL_PORT} -> ${DB_HOST}:5432"
echo "Leave this window open. Press Ctrl+C to close the tunnel."
ssh -i "$KEY" -N \
    -L "${TUNNEL_LOCAL_PORT}:${DB_HOST}:5432" \
    -o "ProxyCommand=aws ec2-instance-connect open-tunnel --instance-id ${INSTANCE_ID}" \
    -o StrictHostKeyChecking=accept-new \
    -o "UserKnownHostsFile=$HOME/.ssh/fintrust_tunnel_known_hosts" \
    -o ServerAliveInterval=30 \
    -o ExitOnForwardFailure=yes \
    "ec2-user@${INSTANCE_ID}"
