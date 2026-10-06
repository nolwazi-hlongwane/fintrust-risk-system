#!/usr/bin/env bash
# Week 13 Day 2: the private network for the database.
#
# What this creates (all free of charge):
#   VPC 10.20.0.0/16 with two PRIVATE subnets in two Availability Zones
#   One route table with NO route to the internet (no internet gateway, no NAT)
#   Gateway endpoints for S3 and DynamoDB (the free way out to those two services)
#   Four security groups, each allowing only the one connection it needs:
#       lambda-sg  -> may open 5432 to rds-sg, and HTTPS to S3 and DynamoDB only
#       rds-sg     <- accepts 5432 from lambda-sg and tunnel-sg only
#       tunnel-sg  <- accepts SSH from eice-sg only; may open 5432 to rds-sg
#       eice-sg    -> may open SSH to tunnel-sg only
#   A DB subnet group telling RDS which subnets it may use
#
# Safe to run more than once.
# Usage:  ./infrastructure/network/deploy_network.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

tags() {  # tags <resource-type> <name>
  echo "ResourceType=$1,Tags=[{Key=Name,Value=$2},{Key=Project,Value=fintrust-capstone}]"
}

# Run an aws command; treat "already exists" as success, anything else as failure.
tolerate() {  # tolerate <error-text-to-accept> <command...>
  local accept="$1"; shift
  local output
  if ! output="$("$@" 2>&1)"; then
    if [[ "$output" == *"$accept"* ]]; then return 0; fi
    echo "$output" >&2
    return 1
  fi
}

# ---------- VPC ----------
VPC_ID="$(vpc_id)"
if [[ -z "$VPC_ID" ]]; then
  VPC_ID="$(aws ec2 create-vpc --cidr-block 10.20.0.0/16 \
      --tag-specifications "$(tags vpc "${PREFIX}-vpc")" --query Vpc.VpcId --output text)"
  aws ec2 wait vpc-available --vpc-ids "$VPC_ID"
  echo "Created VPC $VPC_ID"
else
  echo "VPC exists $VPC_ID"
fi
# RDS endpoints are DNS names, so the VPC must resolve DNS.
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support '{"Value":true}'
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames '{"Value":true}'

# ---------- two private subnets in two Availability Zones ----------
AZ_A="$(aws ec2 describe-availability-zones --filters Name=state,Values=available \
    --query 'sort_by(AvailabilityZones,&ZoneName)[0].ZoneName' --output text)"
AZ_B="$(aws ec2 describe-availability-zones --filters Name=state,Values=available \
    --query 'sort_by(AvailabilityZones,&ZoneName)[1].ZoneName' --output text)"

make_subnet() {  # make_subnet <a|b> <cidr> <az>
  set -e   # bash turns "stop on error" off inside $(...), so switch it back on here
  local id; id="$(subnet_id "$1")"
  if [[ -z "$id" ]]; then
    id="$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "$2" --availability-zone "$3" \
        --tag-specifications "$(tags subnet "${PREFIX}-private-$1")" \
        --query Subnet.SubnetId --output text)"
    echo "Created subnet ${PREFIX}-private-$1 $id in $3" >&2
  fi
  echo "$id"
}
SUBNET_A="$(make_subnet a 10.20.1.0/24 "$AZ_A")"
SUBNET_B="$(make_subnet b 10.20.2.0/24 "$AZ_B")"

# ---------- route table with no internet route ----------
RTB_ID="$(aws ec2 describe-route-tables --filters "Name=tag:Name,Values=${PREFIX}-private-rt" \
    --query 'RouteTables[0].RouteTableId' --output text | none_to_empty)"
if [[ -z "$RTB_ID" ]]; then
  RTB_ID="$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
      --tag-specifications "$(tags route-table "${PREFIX}-private-rt")" \
      --query RouteTable.RouteTableId --output text)"
  echo "Created route table $RTB_ID"
fi
for SUBNET in "$SUBNET_A" "$SUBNET_B"; do
  tolerate "AlreadyAssociated" aws ec2 associate-route-table --route-table-id "$RTB_ID" --subnet-id "$SUBNET"
done

# ---------- gateway endpoints: S3 and DynamoDB without internet or NAT ----------
for SERVICE in s3 dynamodb; do
  SERVICE_NAME="com.amazonaws.${AWS_REGION}.${SERVICE}"
  EXISTING="$(aws ec2 describe-vpc-endpoints \
      --filters "Name=vpc-id,Values=${VPC_ID}" "Name=service-name,Values=${SERVICE_NAME}" \
                "Name=vpc-endpoint-state,Values=available,pending" \
      --query 'VpcEndpoints[0].VpcEndpointId' --output text | none_to_empty)"
  if [[ -z "$EXISTING" ]]; then
    aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" --vpc-endpoint-type Gateway \
        --service-name "$SERVICE_NAME" --route-table-ids "$RTB_ID" \
        --tag-specifications "$(tags vpc-endpoint "${PREFIX}-${SERVICE}-endpoint")" >/dev/null
    echo "Created gateway endpoint for $SERVICE"
  fi
done

# ---------- security groups ----------
make_sg() {  # make_sg <short-name> <description>
  set -e   # bash turns "stop on error" off inside $(...), so switch it back on here
  local id; id="$(sg_id "$1")"
  if [[ -z "$id" ]]; then
    id="$(aws ec2 create-security-group --vpc-id "$VPC_ID" --group-name "${PREFIX}-$1-sg" \
        --description "$2" \
        --tag-specifications "$(tags security-group "${PREFIX}-$1-sg")" \
        --query GroupId --output text)"
    echo "Created security group ${PREFIX}-$1-sg $id" >&2
    # A new security group allows ALL outbound traffic. Remove that, then add
    # back only what is needed.
    aws ec2 revoke-security-group-egress --group-id "$id" \
        --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]' >/dev/null
  fi
  echo "$id"
}
LAMBDA_SG="$(make_sg lambda "FinTrust VPC Lambdas: outbound to RDS, S3 and DynamoDB only")"
RDS_SG="$(make_sg rds "FinTrust RDS: PostgreSQL from Lambda and tunnel only")"
TUNNEL_SG="$(make_sg tunnel "FinTrust DB tunnel instance: SSH from EC2 Instance Connect Endpoint only")"
EICE_SG="$(make_sg eice "FinTrust EC2 Instance Connect Endpoint: SSH to tunnel instance only")"

allow_in()  {  # allow_in <group> <port> <source-group>
  tolerate "InvalidPermission.Duplicate" aws ec2 authorize-security-group-ingress --group-id "$1" \
      --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":$2,\"ToPort\":$2,\"UserIdGroupPairs\":[{\"GroupId\":\"$3\"}]}]"
}
allow_out() {  # allow_out <group> <port> <destination-group>
  tolerate "InvalidPermission.Duplicate" aws ec2 authorize-security-group-egress --group-id "$1" \
      --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":$2,\"ToPort\":$2,\"UserIdGroupPairs\":[{\"GroupId\":\"$3\"}]}]"
}
allow_out_prefix_list() {  # allow_out_prefix_list <group> <prefix-list-id>
  tolerate "InvalidPermission.Duplicate" aws ec2 authorize-security-group-egress --group-id "$1" \
      --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":443,\"ToPort\":443,\"PrefixListIds\":[{\"PrefixListId\":\"$2\"}]}]"
}

allow_in  "$RDS_SG"    5432 "$LAMBDA_SG"
allow_in  "$RDS_SG"    5432 "$TUNNEL_SG"
allow_out "$LAMBDA_SG" 5432 "$RDS_SG"
allow_in  "$TUNNEL_SG" 22   "$EICE_SG"
allow_out "$TUNNEL_SG" 5432 "$RDS_SG"
allow_out "$EICE_SG"   22   "$TUNNEL_SG"

# A "prefix list" is AWS's maintained list of the IP ranges of a service.
# Allowing HTTPS to these two lists lets Lambda reach S3 and DynamoDB through
# the gateway endpoints, and nothing else on the internet.
for SERVICE in s3 dynamodb; do
  PL_ID="$(aws ec2 describe-prefix-lists \
      --filters "Name=prefix-list-name,Values=com.amazonaws.${AWS_REGION}.${SERVICE}" \
      --query 'PrefixLists[0].PrefixListId' --output text | none_to_empty)"
  if [[ -z "$PL_ID" ]]; then
    echo "ERROR: could not find the prefix list for ${SERVICE}" >&2; exit 1
  fi
  allow_out_prefix_list "$LAMBDA_SG" "$PL_ID"
done

# ---------- DB subnet group ----------
if ! aws rds describe-db-subnet-groups --db-subnet-group-name "${PREFIX}-db-subnets" >/dev/null 2>&1; then
  aws rds create-db-subnet-group --db-subnet-group-name "${PREFIX}-db-subnets" \
      --db-subnet-group-description "FinTrust private subnets in two Availability Zones" \
      --subnet-ids "$SUBNET_A" "$SUBNET_B" \
      --tags Key=Project,Value=fintrust-capstone >/dev/null
  echo "Created DB subnet group ${PREFIX}-db-subnets"
fi

echo
echo "Network ready:"
echo "  VPC        $VPC_ID"
echo "  Subnets    $SUBNET_A ($AZ_A)   $SUBNET_B ($AZ_B)"
echo "  Lambda SG  $LAMBDA_SG"
echo "  RDS SG     $RDS_SG"
echo "  Tunnel SG  $TUNNEL_SG"
echo "  EICE SG    $EICE_SG"
