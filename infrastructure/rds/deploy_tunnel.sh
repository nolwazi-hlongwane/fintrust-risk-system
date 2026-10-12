#!/usr/bin/env bash
# Week 13 Day 2: a private way for the administrator to reach the private database.
#
# The database has no public endpoint, so psql on a laptop cannot reach it
# directly. This creates:
#   1. An EC2 Instance Connect Endpoint (free): lets you open an SSH connection
#      into the private subnet, authorised by your IAM identity.
#   2. A tiny EC2 instance (t3.micro) in the private subnet with NO public IP
#      and NO inbound rule from the internet. SSH forwards one port through it
#      to the database.
#
# Nothing is exposed to the internet, and there is no long-lived SSH key: a
# one-time key is pushed for 60 seconds each time you open the tunnel.
# Stop the instance when you are not using it (scripts/rds_stop.sh does this).
#
# Safe to run more than once.
# Usage:  ./infrastructure/rds/deploy_tunnel.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

VPC_ID="$(vpc_id)"; SUBNET_A="$(subnet_id a)"
TUNNEL_SG="$(sg_id tunnel)"; EICE_SG="$(sg_id eice)"
if [[ -z "$VPC_ID" || -z "$SUBNET_A" || -z "$TUNNEL_SG" || -z "$EICE_SG" ]]; then
  echo "ERROR: network not found. Run ./infrastructure/network/deploy_network.sh first." >&2
  exit 1
fi

# ---------- EC2 Instance Connect Endpoint ----------
eice_state() {
  aws ec2 describe-instance-connect-endpoints --filters "Name=vpc-id,Values=${VPC_ID}" \
      --query "InstanceConnectEndpoints[?State!='delete-complete' && State!='delete-in-progress'] | [0].State" \
      --output text | none_to_empty
}
if [[ -z "$(eice_state)" ]]; then
  aws ec2 create-instance-connect-endpoint --subnet-id "$SUBNET_A" --security-group-ids "$EICE_SG" \
      --tag-specifications "ResourceType=instance-connect-endpoint,Tags=[{Key=Name,Value=${PREFIX}-eice},{Key=Project,Value=fintrust-capstone}]" \
      >/dev/null
  echo "Creating EC2 Instance Connect Endpoint (about 5 minutes)..."
fi
while true; do
  STATE="$(eice_state)"
  [[ "$STATE" == "create-complete" ]] && break
  if [[ "$STATE" == "create-failed" || -z "$STATE" ]]; then
    echo "ERROR: Instance Connect Endpoint state is '${STATE}'" >&2; exit 1
  fi
  sleep 15
done
echo "Instance Connect Endpoint ready"

# ---------- tunnel instance ----------
INSTANCE_ID="$(tunnel_instance_id)"
if [[ -z "$INSTANCE_ID" ]]; then
  INSTANCE_ID="$(aws ec2 run-instances \
      --image-id "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64" \
      --instance-type t3.micro \
      --subnet-id "$SUBNET_A" --security-group-ids "$TUNNEL_SG" \
      --no-associate-public-ip-address \
      --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${TUNNEL_NAME}},{Key=Project,Value=fintrust-capstone}]" \
      --query 'Instances[0].InstanceId' --output text)"
  echo "Created tunnel instance $INSTANCE_ID"
else
  echo "Tunnel instance exists $INSTANCE_ID"
  aws ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null
fi
aws ec2 wait instance-status-ok --instance-ids "$INSTANCE_ID"

echo
echo "Tunnel ready. Open it with:  ./scripts/db_tunnel.sh"
