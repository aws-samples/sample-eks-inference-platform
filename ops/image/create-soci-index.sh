#!/usr/bin/env bash
# Build and push a SOCI index for an ECR image using a temporary EC2 instance.
# Usage: ./ops/image/create-soci-index.sh [-p <instance-profile>] [-n <cluster>] [-r <region>] <ecr-image-uri>
# Example: ./ops/image/create-soci-index.sh <account-id>.dkr.ecr.<region>.amazonaws.com/docker-hub/vllm/vllm-openai:v0.24.0
#
# The temp instance must run under an instance profile that can BOTH pull and
# PUSH to ECR (soci push uploads the index as a referrer artifact). The EKS
# node role only has ECR *read*, so `soci push` 403s. Pass -p with a push-capable
# profile (Terraform provisions `<cluster>-soci-builder` for this); without -p we
# fall back to the first running node's profile (read-only — push will fail
# unless that role was granted ECR write).
#
# Networking + cluster name resolve HERMETICALLY from AWS APIs (no kubectl): the
# subnet is found by the `karpenter.sh/discovery=<cluster>` tag, matching
# create-data-volume-snapshot.sh. This avoids racing the host kubeconfig during a
# `terraform apply` on a brand-new cluster (where the host context may still
# point elsewhere). Pass -n/-r explicitly from Terraform; both fall back to the
# kubeconfig / AWS_REGION / the image URI when omitted (manual ad-hoc use).
set -euo pipefail

INSTANCE_PROFILE=""
CLUSTER_NAME=""
REGION=""
while getopts "p:n:r:h" opt; do
  case "$opt" in
    p) INSTANCE_PROFILE="$OPTARG" ;;
    n) CLUSTER_NAME="$OPTARG" ;;
    r) REGION="$OPTARG" ;;
    h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option" >&2; exit 1 ;;
  esac
done
shift $((OPTIND - 1))

IMAGE="${1:?Usage: $0 [-p <instance-profile>] [-n <cluster>] [-r <region>] <ecr-image-uri>}"

# Validate the image URI up front: it is interpolated into a shell script that
# runs as ROOT on the temporary builder (via `aws ssm send-command`), so a URI
# containing shell metacharacters (;, $(), backticks, spaces) would be command
# injection under a push-capable ECR role. Restrict to the characters a real
# image reference uses. (Matches create-data-volume-snapshot.sh.)
IMAGE_PATTERN='^[a-zA-Z0-9._:/@-]+$'
if [[ ! "$IMAGE" =~ $IMAGE_PATTERN ]]; then
  echo "Error: invalid characters in image URI: $IMAGE" >&2
  echo "  Image URIs must match: $IMAGE_PATTERN" >&2
  exit 1
fi

# Parse account + region STRUCTURALLY from the ECR registry host
# (<acct>.dkr.ecr.<region>.amazonaws.com[.cn]/...), rather than regex-scraping
# the whole URI — a repo path segment like "us-west-2" or a govcloud region
# ("us-gov-west-1", which the old '[a-z]+-[a-z]+-[0-9]+' regex mangled to
# "gov-west-1") both broke that. The host is the first path segment.
IMAGE_HOST="${IMAGE%%/*}"
ACCOUNT=""
REGION_FROM_IMAGE=""
case "$IMAGE_HOST" in
  *.dkr.ecr.*.amazonaws.com | *.dkr.ecr.*.amazonaws.com.cn)
    ACCOUNT="${IMAGE_HOST%%.*}"                       # field 1
    REGION_FROM_IMAGE="$(echo "$IMAGE_HOST" | cut -d. -f4)"  # <acct>.dkr.ecr.<REGION>...
    ;;
esac

# REGION precedence: -r flag > AWS_REGION env > parsed from the ECR host.
REGION="${REGION:-${AWS_REGION:-$REGION_FROM_IMAGE}}"
if [ -z "$REGION" ]; then
  echo "Error: cannot determine region — pass -r <region> or set AWS_REGION." >&2
  exit 1
fi
# ACCOUNT is required to build the ECR registry login below. Fail with a clear
# message for a non-ECR URI instead of dying mid-script under `set -e`.
if [ -z "$ACCOUNT" ]; then
  echo "Error: '$IMAGE' is not an ECR image URI (expected" >&2
  echo "  <account-id>.dkr.ecr.<region>.amazonaws.com/...). This tool builds and" >&2
  echo "  pushes a SOCI index to ECR, so only ECR images are supported." >&2
  exit 1
fi

# Self-protect: if the target image isn't in ECR yet, there's nothing to index.
# This makes the script safe to call from Terraform when an image isn't in ECR
# yet: we must NOT launch a builder and fail trying to pull a tag that doesn't
# exist. Exit 0 (success, no-op) instead.
# Only applies to ECR repo URIs (<acct>.dkr.ecr.<region>...); skip the check for
# any other registry.
if echo "$IMAGE" | grep -qE '\.dkr\.ecr\.'; then
  REPO_NAME="${IMAGE#*/}"; REPO_NAME="${REPO_NAME%%:*}"
  IMG_TAG="${IMAGE##*:}"
  if ! aws ecr describe-images --region "$REGION" \
        --repository-name "$REPO_NAME" --image-ids "imageTag=${IMG_TAG}" \
        >/dev/null 2>&1; then
    echo "⚠ Image not found in ECR: ${IMAGE} — skipping SOCI index (nothing to index)." >&2
    echo "  (Build/push the image first, then re-run.)" >&2
    exit 0
  fi
fi

# Cluster name: -n flag wins; otherwise fall back to the host kubeconfig (ad-hoc
# use). From Terraform we always pass -n so we never read the host context.
if [ -z "$CLUSTER_NAME" ]; then
  CLUSTER_NAME=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}' 2>/dev/null | awk -F/ '{print $NF}')
fi
if [ -z "$CLUSTER_NAME" ]; then
  echo "Error: cannot determine cluster name. Pass -n <cluster>." >&2
  exit 1
fi

# Default to the running node's instance profile only if -p wasn't supplied.
# NOTE: the node profile has ECR read but NOT push — prefer -p <push-capable>.
if [ -z "$INSTANCE_PROFILE" ]; then
  INSTANCE_PROFILE=$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:eks:cluster-name,Values=$CLUSTER_NAME" "Name=instance-state-name,Values=running" \
    --query "Reservations[0].Instances[0].IamInstanceProfile.Arn" --output text | awk -F/ '{print $NF}')
  echo "⚠ No -p given; using node profile '$INSTANCE_PROFILE' (ECR read-only — 'soci push' may 403)." >&2
fi

# Subnet: resolved hermetically via the Karpenter discovery tag (no kubectl), the
# same way create-data-volume-snapshot.sh does — robust on a fresh-cluster apply.
SUBNET=$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=tag:karpenter.sh/discovery,Values=$CLUSTER_NAME" \
  --query "Subnets[0].SubnetId" --output text)
if [ -z "$SUBNET" ] || [ "$SUBNET" = "None" ]; then
  echo "Error: no subnet found with tag karpenter.sh/discovery=$CLUSTER_NAME in $REGION." >&2
  exit 1
fi

SG=$(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
  --query "cluster.resourcesVpcConfig.clusterSecurityGroupId" --output text)

echo "→ Launching temporary AL2023 instance for SOCI index build..."
INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
  --image-id resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --instance-type m5.large \
  --subnet-id "$SUBNET" \
  --security-group-ids "$SG" \
  --iam-instance-profile Name="$INSTANCE_PROFILE" \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":200,"VolumeType":"gp3","Encrypted":true}}]' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=soci-index-builder},{Key=Purpose,Value=temporary}]" \
  --query "Instances[0].InstanceId" --output text)
echo "  Instance: $INSTANCE_ID"

cleanup() {
  echo "→ Terminating temporary instance $INSTANCE_ID..."
  aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$REGION" > /dev/null
  echo "  Done."
}
trap cleanup EXIT

echo "→ Waiting for instance to be ready..."
aws ec2 wait instance-status-ok --instance-ids "$INSTANCE_ID" --region "$REGION"

echo "→ Installing tools and building SOCI index..."
CMD_ID=$(aws ssm send-command --instance-ids "$INSTANCE_ID" --region "$REGION" \
  --document-name "AWS-RunShellScript" \
  --timeout-seconds 1800 \
  --parameters "commands=[
    \"set -ex\",
    \"yum install -y containerd nerdctl soci-snapshotter\",
    \"systemctl start containerd\",
    \"aws ecr get-login-password --region $REGION | nerdctl login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com\",
    \"nerdctl pull --platform linux/amd64 $IMAGE\",
    \"export TMPDIR=/var/tmp\",
    \"soci create $IMAGE\",
    \"soci push $IMAGE\",
    \"echo SOCI_INDEX_COMPLETE\"
  ]" \
  --query "Command.CommandId" --output text)

# Bounded poll: the SSM command has an 1800s server-side timeout, so give it a
# wall-clock deadline with margin (150 × 15s = ~37 min) rather than looping
# forever. Without a bound, an instance that never registers with SSM makes
# get-command-invocation error indefinitely — masked as "Pending" by the
# fallback below — and a Terraform-invoked run would wedge the apply.
MAX_POLLS=150
STATUS="Pending"
for _ in $(seq 1 "$MAX_POLLS"); do
  sleep 15
  STATUS=$(aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" --region "$REGION" \
    --query "Status" --output text 2>/dev/null || echo "Pending")
  case "$STATUS" in
    Success)
      echo "✓ SOCI index created and pushed for $IMAGE"
      exit 0 ;;
    Failed|TimedOut|Cancelled)
      echo "✗ Failed (status: $STATUS). Output:"
      aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" --region "$REGION" \
        --query "StandardErrorContent" --output text
      exit 1 ;;
    *)
      echo "  $(date +%H:%M:%S) $STATUS..." ;;
  esac
done

echo "✗ Timed out after ~$((MAX_POLLS * 15))s waiting for the SSM command (last status: ${STATUS})." >&2
echo "  The instance may not have registered with SSM (check the instance profile has the SSM managed policy)." >&2
exit 1
