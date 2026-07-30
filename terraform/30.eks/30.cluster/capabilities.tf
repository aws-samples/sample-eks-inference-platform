################################################################################
# EKS Managed Capabilities - IAM Roles
################################################################################
locals {
  enabled_capabilities = {
    for k, v in {
      argocd = { type = "ARGOCD", enabled = local.capabilities.gitops }
      kro    = { type = "KRO", enabled = local.capabilities.kro }
      ack    = { type = "ACK", enabled = local.capabilities.ack }
    } : k => v if v.enabled
  }
}

data "aws_iam_policy_document" "capability_trust" {
  count = length(local.enabled_capabilities) > 0 ? 1 : 0

  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["capabilities.eks.amazonaws.com"]
    }
    actions = ["sts:AssumeRole", "sts:TagSession"]
  }
}

resource "aws_iam_role" "capability" {
  for_each = local.enabled_capabilities

  name               = "${local.cluster_name}-capability-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.capability_trust[0].json
  tags               = local.tags
}

################################################################################
# EKS Managed Capabilities - ArgoCD (requires Identity Center config)
################################################################################
resource "aws_eks_capability" "argocd" {
  count = local.capabilities.gitops ? 1 : 0

  cluster_name              = module.eks.cluster_name
  capability_name           = "argocd"
  type                      = "ARGOCD"
  role_arn                  = aws_iam_role.capability["argocd"].arn
  delete_propagation_policy = "RETAIN"
  tags                      = local.tags

  configuration {
    argo_cd {
      namespace = "argocd"
      aws_idc {
        idc_instance_arn = var.cluster_config.capabilities_config.argocd_idc_instance_arn
        idc_region       = try(var.cluster_config.capabilities_config.argocd_idc_region, null)
      }
      dynamic "rbac_role_mapping" {
        for_each = try(var.cluster_config.capabilities_config.argocd_rbac_mappings, [])
        content {
          role = rbac_role_mapping.value.role
          dynamic "identity" {
            for_each = rbac_role_mapping.value.identities
            content {
              id   = identity.value.id
              type = identity.value.type
            }
          }
        }
      }
    }
  }

  depends_on = [module.eks]
}

################################################################################
# EKS Managed Capabilities - KRO and ACK (no extra config needed)
################################################################################
resource "aws_eks_capability" "simple" {
  for_each = {
    for k, v in local.enabled_capabilities : k => v
    if k != "argocd"
  }

  cluster_name              = module.eks.cluster_name
  capability_name           = each.key
  type                      = each.value.type
  role_arn                  = aws_iam_role.capability[each.key].arn
  delete_propagation_policy = "RETAIN"
  tags                      = local.tags

  depends_on = [module.eks]
}

# KRO needs cluster-admin level access to create/manage arbitrary resources
# defined in ResourceGraphDefinitions (Deployments, Services, etc.)
resource "aws_eks_access_policy_association" "kro_edit" {
  count = local.capabilities.kro ? 1 : 0

  cluster_name  = module.eks.cluster_name
  principal_arn = aws_iam_role.capability["kro"].arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_capability.simple]
}

################################################################################
# ECR Pull-Through Cache (optional) — mirror Docker Hub images to private ECR
# Only created when docker_hub_credentials is provided in tfvars.
# Without it, KRO falls back to pulling directly from Docker Hub.
################################################################################
resource "aws_secretsmanager_secret" "docker_hub" {
  # CKV_AWS_149: this is an ECR pull-through cache credential secret (name must
  # start with "ecr-pullthroughcache/"). Amazon ECR does NOT support a
  # customer-managed KMS key for these secrets — it requires the default
  # aws/secretsmanager key, so a CMK would break the pull-through cache.
  # https://docs.aws.amazon.com/AmazonECR/latest/userguide/pull-through-cache-creating-secret.html
  #checkov:skip=CKV_AWS_149:ECR pull-through cache secrets must use the default aws/secretsmanager key; ECR does not support a CMK for them.
  #checkov:skip=CKV2_AWS_57:This holds a static Docker Hub access token supplied by the operator; there is no AWS-native automatic rotation for Docker Hub credentials.
  count                   = var.docker_hub_username != "" ? 1 : 0
  name                    = "ecr-pullthroughcache/docker-hub"
  recovery_window_in_days = 0
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "docker_hub" {
  count     = var.docker_hub_username != "" ? 1 : 0
  secret_id = aws_secretsmanager_secret.docker_hub[0].id
  secret_string = jsonencode({
    username    = var.docker_hub_username
    accessToken = var.docker_hub_access_token
  })
}

resource "aws_ecr_pull_through_cache_rule" "docker_hub" {
  count                 = var.docker_hub_username != "" ? 1 : 0
  ecr_repository_prefix = "docker-hub"
  upstream_registry_url = "registry-1.docker.io"
  credential_arn        = aws_secretsmanager_secret.docker_hub[0].arn
}

# Auto-create the cached repos on first pull. Without a creation template the
# FIRST pull through the cache also requires ecr:CreateRepository on the puller;
# the template lets ECR create docker-hub/* repos itself, so node/builder roles
# only need ecr:BatchImportUpstreamImage (granted below), not CreateRepository.
resource "aws_ecr_repository_creation_template" "docker_hub" {
  count                = var.docker_hub_username != "" ? 1 : 0
  prefix               = "docker-hub"
  applied_for          = ["PULL_THROUGH_CACHE"]
  description          = "Auto-create pull-through cache repos for docker-hub/*"
  image_tag_mutability = "MUTABLE"

  depends_on = [aws_ecr_pull_through_cache_rule.docker_hub]
}

# Pull-through-cache import permission for principals that PULL docker-hub/*
# images: a fresh-account first pull needs ecr:BatchImportUpstreamImage (and,
# absent the creation template above, ecr:CreateRepository). The standard
# read-only node policies (AmazonEC2ContainerRegistryReadOnly/PullOnly) lack it,
# so without this the very first vLLM image pull — at apply time on the SOCI/snapshot
# builder, and at runtime on GPU nodes — 403s and breaks model serving.
resource "aws_iam_policy" "ecr_pull_through" {
  count       = var.docker_hub_username != "" ? 1 : 0
  name        = "${local.cluster_name}-ecr-pull-through-import"
  description = "Allow importing upstream images into the docker-hub/* pull-through cache"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "PullThroughImport"
      Effect = "Allow"
      # ecr:CreateRepository is intentionally NOT granted: the
      # aws_ecr_repository_creation_template above lets ECR create the
      # docker-hub/* cache repos itself on first pull, so pullers only need
      # BatchImportUpstreamImage. This policy is attached to every Karpenter GPU
      # node, so dropping CreateRepository stops nodes from creating arbitrary
      # docker-hub/* repositories.
      Action = [
        "ecr:BatchImportUpstreamImage",
      ]
      Resource = "arn:aws:ecr:${local.region}:${data.aws_caller_identity.current.account_id}:repository/docker-hub/*"
    }]
  })
}

################################################################################
# Platform Config — KRO externalRef reads this ConfigMap.
# vllmImage always set: ECR mirror when the pull-through cache is enabled, Docker
# Hub otherwise. Version is defined once in locals.tf (vllm_image_tag).
# modelCacheBucket + region are consumed by the serving worker's initContainer
# and auto-warm sidecar to sync HF weights to/from s3://{bucket}/hf/...
################################################################################
resource "kubernetes_config_map" "platform_config" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "platform-config"
    namespace = "inference"
  }

  data = {
    cluster = module.eks.cluster_name
    region  = local.region
    # vLLM serving image via the docker-hub pull-through cache (falls back to the
    # plain Docker Hub ref when no ECR cache is configured). The serving-tier RGDs
    # (VLLMEndpoint/LLMDEndpoint/LLMDDisaggEndpoint) read this via externalRef,
    # falling back to their schema default.
    vllmImage        = var.docker_hub_username != "" ? "${data.aws_caller_identity.current.account_id}.dkr.ecr.${local.region}.amazonaws.com/docker-hub/${local.vllm_image}" : local.vllm_image
    modelCacheBucket = local.capabilities.gitops ? aws_s3_bucket.model_cache[0].bucket : ""
  }

  depends_on = [kubernetes_namespace.inference]
}

################################################################################
# Model weights cache — S3 bucket + IRSA role for vLLM worker pods.
#
# The initContainer in the serving-tier RGDs s5cmd-syncs HF
# weights from this bucket on pod startup (cache hit) or falls back to a live
# HuggingFace download (cache miss). An auto-warm sidecar uploads new models
# after vLLM finishes loading, so subsequent deploys hit the cache.
#
# Bucket layout: s3://{bucket}/hf/{org}/{model}/...   (mirrors HF cache tree)
################################################################################

resource "aws_s3_bucket" "model_cache" {
  count         = local.capabilities.gitops ? 1 : 0
  bucket        = local.model_cache_bucket
  force_destroy = true # cache is regeneratable; safe to destroy with the cluster

  tags = merge(local.tags, { Purpose = "hf-model-weights-cache" })
}

resource "aws_s3_bucket_public_access_block" "model_cache" {
  count                   = local.capabilities.gitops ? 1 : 0
  bucket                  = aws_s3_bucket.model_cache[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "model_cache" {
  count  = local.capabilities.gitops ? 1 : 0
  bucket = aws_s3_bucket.model_cache[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "model_cache" {
  count  = local.capabilities.gitops ? 1 : 0
  bucket = aws_s3_bucket.model_cache[0].id

  rule {
    id     = "transition-stale-to-ia"
    status = "Enabled"

    filter { prefix = "hf/" }

    transition {
      days          = 60
      storage_class = "STANDARD_IA"
    }
  }
}

# IAM role scoped to the inference-worker ServiceAccount via OIDC.
# Only vLLM worker pods that use this SA can read/write the cache bucket.
resource "aws_iam_role" "inference_worker" {
  count = local.capabilities.gitops ? 1 : 0
  name  = "${local.cluster_name}-inference-worker"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${module.eks.oidc_provider}:sub" = "system:serviceaccount:${local.inference_worker_sa_namespace}:${local.inference_worker_sa_name}"
          "${module.eks.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy" "inference_worker_s3_cache" {
  count = local.capabilities.gitops ? 1 : 0
  name  = "model-cache-access"
  role  = aws_iam_role.inference_worker[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # HF weight cache: full read/write (the auto-warm sidecar populates it).
        Sid      = "HfCacheReadWrite"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.model_cache[0].arn}/hf/*"
      },
      {
        # Model-weight cache: READ-only. Each serving tier's init container syncs
        # pre-seeded HuggingFace model weights (s3://<bucket>/hf/<model-id>/) from
        # this bucket on startup for a fast cold start, falling back to a HF
        # download on a cache miss. Read-only — serving pods never write weights.
        Sid      = "ModelCacheReadOnly"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.model_cache[0].arn}/fine-tuned/*"
      },
      {
        # ListBucket for both prefixes — s5cmd sync of "prefix/*" needs List.
        Sid      = "ListModelCache"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.model_cache[0].arn
        Condition = {
          StringLike = { "s3:prefix" = ["hf/*", "fine-tuned/*", ""] }
        }
      },
    ]
  })
}

# The ServiceAccount the vLLM serving worker pods reference. Annotation ties it
# to the IAM role via IRSA. Kept Terraform-managed so the ARN is injected at
# apply time without hardcoding account IDs in git-tracked YAML.
resource "kubernetes_service_account" "inference_worker" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = local.inference_worker_sa_name
    namespace = local.inference_worker_sa_namespace
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.inference_worker[0].arn
    }
    labels = {
      "app.kubernetes.io/part-of" = "ai-platform"
    }
  }

  depends_on = [kubernetes_namespace.inference]
}

################################################################################
# ArgoCD — cluster-admin access for deploying applications locally
# Ref: https://docs.aws.amazon.com/eks/latest/userguide/argocd-register-clusters.html
################################################################################
resource "aws_eks_access_policy_association" "argocd_admin" {
  count = local.capabilities.gitops ? 1 : 0

  cluster_name  = module.eks.cluster_name
  principal_arn = aws_iam_role.capability["argocd"].arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_capability.argocd]
}

################################################################################
# ArgoCD — register the local cluster as a deployment target
# The managed ArgoCD capability does NOT auto-register the local cluster.
# Uses the EKS cluster ARN as server (kubernetes.default.svc is not supported).
################################################################################
resource "kubernetes_secret" "argocd_cluster" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "local-cluster"
    namespace = "argocd"
    labels = {
      "argocd.argoproj.io/secret-type" = "cluster"
    }
  }

  data = {
    name   = "local-cluster"
    server = module.eks.cluster_arn
  }

  depends_on = [aws_eks_capability.argocd]
}

################################################################################
# ArgoCD — bootstrap Application.
# Points at argocd/bootstrap/ in the git repo — a Helm chart (app-of-apps) whose
# templates are the platform + workloads ApplicationSets. This is the ONLY ArgoCD
# Application rendered by Terraform; it passes the repo URL(s) down as Helm values
# so every ApplicationSet inherits them. Forking the platform = set
# var.gitops_repo_url in tfvars — ONE place. (Optionally point self-service
# workloads at a separate repo with var.gitops_workloads_repo_url.)
################################################################################
resource "kubectl_manifest" "argocd_bootstrap" {
  count = local.capabilities.gitops ? 1 : 0

  yaml_body = templatefile("${path.module}/argocd/bootstrap.yaml.tpl", {
    repo_url           = var.gitops_repo_url
    revision           = var.gitops_revision
    workloads_repo_url = var.gitops_workloads_repo_url
  })

  lifecycle {
    precondition {
      condition     = var.gitops_repo_url != ""
      error_message = "var.gitops_repo_url must be set when cluster_config.capabilities.gitops is enabled."
    }
  }

  depends_on = [
    aws_eks_capability.argocd,
    kubernetes_secret.argocd_cluster,
  ]
}

################################################################################
# Namespaces — create before secrets so Terraform doesn't fail on fresh clusters
################################################################################
resource "kubernetes_namespace" "ai_platform" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name   = "ai-platform"
    labels = { "app.kubernetes.io/part-of" = "ai-platform" }
  }

  depends_on = [aws_eks_capability.argocd]
}

resource "kubernetes_namespace" "inference" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name = "inference"
    labels = {
      "app.kubernetes.io/part-of" = "ai-platform"
      "purpose"                   = "inference-endpoints"
    }
  }

  depends_on = [aws_eks_capability.argocd]
}

################################################################################
# LiteLLM — pre-create master key secret
################################################################################
resource "random_password" "litellm_master_key" {
  count   = local.capabilities.gitops ? 1 : 0
  length  = 32
  special = false
}

resource "kubernetes_secret" "litellm_secrets" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "litellm-secrets"
    namespace = "ai-platform"
  }

  data = {
    master-key = random_password.litellm_master_key[0].result
  }

  depends_on = [kubernetes_namespace.ai_platform]
}

################################################################################
# Platform DB — shared PostgreSQL credentials (used by LiteLLM and Langfuse)
################################################################################
resource "random_password" "platform_db_password" {
  count   = local.capabilities.gitops ? 1 : 0
  length  = 24
  special = false
}

resource "kubernetes_secret" "platform_db_credentials" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "platform-db-credentials"
    namespace = "ai-platform"
  }

  data = {
    username     = "platform"
    password     = random_password.platform_db_password[0].result
    litellm-url  = "postgres://platform:${random_password.platform_db_password[0].result}@platform-db:5432/litellm"
    langfuse-url = "postgres://platform:${random_password.platform_db_password[0].result}@platform-db:5432/langfuse"
  }

  depends_on = [kubernetes_namespace.ai_platform]
}

# LiteLLM API key in inference namespace — used by KRO registration Jobs
resource "kubernetes_secret" "litellm_api_key" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "litellm-api-key"
    namespace = "inference"
  }

  data = {
    master-key = random_password.litellm_master_key[0].result
  }

  depends_on = [kubernetes_namespace.inference]
}

################################################################################
# Langfuse — pre-create secrets so the Helm chart can reference them
################################################################################
resource "random_password" "langfuse" {
  for_each = local.capabilities.gitops ? toset([
    "password", "clickhouse-password", "redis-password", "s3-secret-key"
  ]) : toset([])

  length  = 32
  special = false
}

# Langfuse requires hex-encoded keys for encryption
resource "random_id" "langfuse_encryption_key" {
  count       = local.capabilities.gitops ? 1 : 0
  byte_length = 32
}

resource "random_id" "langfuse_nextauth_secret" {
  count       = local.capabilities.gitops ? 1 : 0
  byte_length = 32
}

resource "random_id" "langfuse_salt" {
  count       = local.capabilities.gitops ? 1 : 0
  byte_length = 16
}

resource "kubernetes_secret" "langfuse_secrets" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "langfuse-secrets"
    namespace = "ai-platform"
  }

  data = merge(
    { for k, v in random_password.langfuse : k => v.result },
    {
      "encryption-key"  = random_id.langfuse_encryption_key[0].hex
      "nextauth-secret" = random_id.langfuse_nextauth_secret[0].hex
      "salt"            = random_id.langfuse_salt[0].hex
      "root-user"       = "minio"
    }
  )

  depends_on = [kubernetes_namespace.ai_platform]
}
