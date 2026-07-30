################################################################################
# GPU Image Optimization — SOCI index + EBS data volume snapshot
#
# Automatically triggered when vllm_image_tag changes in locals.tf.
# Creates both optimization artifacts in the correct order:
#   1. SOCI index in ECR (enables lazy-loading for fallback/new layers)
#   2. EBS snapshot with pre-pulled image (0s image pull on new nodes)
#
# The snapshot ID is discovered by tags, so no manual tfvar needed.
# Set gpu_data_volume_snapshot_id to override with a specific snapshot.
#
# The image baked is the vLLM serving image (vllm/vllm-openai) used by all three
# serving tiers (VLLMEndpoint / LLMDEndpoint / LLMDDisaggEndpoint). It is pulled
# through the docker-hub/* ECR pull-through cache so nodes pull from ECR (and,
# via the snapshot below, from local disk) rather than Docker Hub.
################################################################################

locals {
  # Full ECR (pull-through cache) image URI for the vLLM serving image. Empty
  # when no Docker Hub creds are configured (image optimization then disabled).
  vllm_ecr_image = var.docker_hub_username != "" ? "${data.aws_caller_identity.current.account_id}.dkr.ecr.${local.region}.amazonaws.com/docker-hub/${local.vllm_image}" : ""

  # Whether to run image optimization (requires ECR pull-through cache).
  run_image_optimization = local.capabilities.autoscaling && var.docker_hub_username != ""

  # The push-capable SOCI builder profile is needed whenever the SOCI index is
  # built (run_image_optimization). It pushes the index back to ECR, which the
  # read-only EKS node role can't do (would 403 and abort the apply).
  need_soci_builder = local.run_image_optimization
}

################################################################################
# SOCI Index — enables lazy-loading of container image layers
################################################################################
resource "null_resource" "soci_index" {
  count = local.run_image_optimization ? 1 : 0

  triggers = {
    vllm_image = local.vllm_ecr_image
  }

  provisioner "local-exec" {
    command     = "${path.module}/../../../ops/image/create-soci-index.sh -p ${aws_iam_instance_profile.soci_builder[0].name} -n ${local.cluster_name} -r ${local.region} ${local.vllm_ecr_image}"
    interpreter = ["bash", "-c"]
    # Best-effort: SOCI is a cold-start optimization, not load-bearing. A
    # transient builder failure must degrade to lazy-pull, not abort the apply.
    on_failure = continue
    environment = {
      AWS_REGION = local.region
    }
  }

  depends_on = [
    aws_ecr_pull_through_cache_rule.docker_hub,
    module.eks,
    # Ensure the host kubeconfig points at THIS cluster before the script runs.
    null_resource.update_kubeconfig,
  ]
}

################################################################################
# EBS Data Volume Snapshot — pre-pulled image for instant cold starts
#
# Bakes the vLLM serving image onto the Bottlerocket data volume so new GPU
# nodes boot with it already on disk (0s pull). This is the primary mechanism
# for the large serving image — SOCI lazy-pull is only the fallback (and the
# Bottlerocket SOCI snapshotter is unreliable on multi-GB-layer images).
################################################################################
locals {
  snapshot_images     = local.vllm_ecr_image
  snapshot_volume_gib = 200
}

resource "null_resource" "gpu_data_volume_snapshot" {
  count = local.run_image_optimization ? 1 : 0

  triggers = {
    vllm_image   = local.vllm_ecr_image
    cluster_name = local.cluster_name
  }

  provisioner "local-exec" {
    command     = "${path.module}/../../../ops/image/create-data-volume-snapshot.sh -r ${local.region} -n ${local.cluster_name} -s ${local.snapshot_volume_gib} ${local.snapshot_images}"
    interpreter = ["bash", "-c"]
    # Best-effort cold-start optimization — never abort the platform apply.
    on_failure = continue
    environment = {
      AWS_REGION = local.region
    }
  }

  depends_on = [
    null_resource.soci_index,
    module.eks,
    module.karpenter,
    helm_release.karpenter,
    # create-data-volume-snapshot.sh drives kubectl (schedules a puller pod);
    # the kubeconfig must already point at this cluster.
    null_resource.update_kubeconfig,
  ]
}

################################################################################
# Discover the latest snapshot by tags (avoids manual tfvar management)
#
# On the FIRST apply for a new cluster the snapshot doesn't exist yet (the
# null_resource creates it during apply, but data sources resolve at plan time).
# So this MUST tolerate zero matches without erroring — the singular
# `aws_ebs_snapshot` data source raises "Your query returned no results" on an
# empty match and would abort `terraform apply` on a brand-new account. We use
# the list-returning `aws_ebs_snapshot_ids` (empty list, no error) to gate, then
# read the singular source only when ≥1 snapshot exists (so we still get
# most_recent ordering). First deploy → SOCI-only; second deploy picks up the
# snapshot.
################################################################################
data "aws_ebs_snapshot_ids" "gpu_data_volume" {
  count  = local.run_image_optimization && var.gpu_data_volume_snapshot_id == "" ? 1 : 0
  owners = ["self"]

  filter {
    name   = "tag:Cluster"
    values = [local.cluster_name]
  }
  filter {
    name   = "tag:Purpose"
    values = ["bottlerocket-data-volume"]
  }
  filter {
    name   = "status"
    values = ["completed"]
  }
}

locals {
  # True only once at least one matching snapshot exists (zero on first apply).
  gpu_snapshot_exists = try(length(data.aws_ebs_snapshot_ids.gpu_data_volume[0].ids) > 0, false)
}

# Singular lookup for most_recent ordering — read ONLY when a snapshot exists,
# so a fresh account (zero matches) never triggers the no-results error.
data "aws_ebs_snapshot" "gpu_data_volume" {
  count = local.run_image_optimization && var.gpu_data_volume_snapshot_id == "" && local.gpu_snapshot_exists ? 1 : 0

  most_recent  = true
  owners       = ["self"]
  snapshot_ids = data.aws_ebs_snapshot_ids.gpu_data_volume[0].ids
}

locals {
  # Resolve snapshot ID: explicit override > auto-discovered > empty (disabled).
  # Empty string means no snapshot — Karpenter NodeClasses omit the snapshotID field.
  resolved_snapshot_id = var.gpu_data_volume_snapshot_id != "" ? var.gpu_data_volume_snapshot_id : try(data.aws_ebs_snapshot.gpu_data_volume[0].id, "")
}

################################################################################
# SOCI builder IAM role + instance profile
#
# create-soci-index.sh launches a short-lived EC2 instance that pulls an image,
# builds the SOCI index, and PUSHES it back to ECR as a referrer artifact.
# Pushing needs ECR write (ecr:InitiateLayerUpload, ...) which the EKS node role
# deliberately lacks. This dedicated profile grants exactly the ECR pull+push
# actions on this account's repos (the SOCI builder is ephemeral and
# self-terminates), plus SSM core so the script can drive it via Run Command.
################################################################################
resource "aws_iam_role" "soci_builder" {
  count = local.need_soci_builder ? 1 : 0
  name  = "${local.cluster_name}-soci-builder"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy" "soci_builder_ecr" {
  count = local.need_soci_builder ? 1 : 0
  name  = "ecr-pull-push"
  role  = aws_iam_role.soci_builder[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Auth token is account-wide (no resource scoping allowed).
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        # Pull + push the SOCI index (referrer artifact). Scoped to the
        # docker-hub/* pull-through cache repos this builder actually touches —
        # NOT repository/*, which let a builder compromise overwrite ANY image in
        # the account (supply-chain risk). The PullThroughImport stanza below is
        # already docker-hub/*-scoped for the same reason.
        Sid    = "EcrPullPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
        ]
        Resource = "arn:aws:ecr:${local.region}:${data.aws_caller_identity.current.account_id}:repository/docker-hub/*"
      },
      {
        # The SOCI builder is the apply-time FIRST puller of the vLLM image
        # through the docker-hub/* pull-through cache, so it must be able to
        # import the upstream image (a fresh-account first pull 403s otherwise).
        Sid    = "PullThroughImport"
        Effect = "Allow"
        Action = [
          "ecr:BatchImportUpstreamImage",
          "ecr:CreateRepository",
        ]
        Resource = "arn:aws:ecr:${local.region}:${data.aws_caller_identity.current.account_id}:repository/docker-hub/*"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "soci_builder_ssm" {
  count      = local.need_soci_builder ? 1 : 0
  role       = aws_iam_role.soci_builder[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "soci_builder" {
  count = local.need_soci_builder ? 1 : 0
  name  = "${local.cluster_name}-soci-builder"
  role  = aws_iam_role.soci_builder[0].name

  tags = local.tags
}
