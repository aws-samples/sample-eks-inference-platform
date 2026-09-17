################################################################################
# Bedrock as a model — LiteLLM ServiceAccount + IRSA
#
# LiteLLM serves Bedrock models (e.g. Claude Opus 4.8) natively. Because
# Bedrock models are static (nothing to deploy/scale), they go straight into the
# LiteLLM config (platform/services/litellm/litellm.yaml) — no serving CR,
# no registration Job. The only infra needed is AWS credentials for the LiteLLM
# pod, granted via IRSA on a dedicated `litellm` ServiceAccount.
#
# The SA is always created (the LiteLLM Deployment references it by name); the
# IRSA role + annotation are only attached when var.enable_bedrock = true.
# Mirrors the inference_worker IRSA pattern in capabilities.tf.
################################################################################

locals {
  # IRSA only makes sense when both the platform (gitops) and Bedrock are on.
  enable_bedrock = local.capabilities.gitops && var.enable_bedrock
}

resource "aws_iam_role" "litellm_bedrock" {
  count = local.enable_bedrock ? 1 : 0
  name  = "${local.cluster_name}-litellm-bedrock"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${module.eks.oidc_provider}:sub" = "system:serviceaccount:ai-platform:litellm"
          "${module.eks.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy" "litellm_bedrock" {
  count = local.enable_bedrock ? 1 : 0
  name  = "bedrock-invoke"
  role  = aws_iam_role.litellm_bedrock[0].id

  # Scoped to Bedrock MODEL invocation only — foundation models (ARNs carry an
  # empty account) plus this account's inference profiles (system-defined
  # cross-region profiles like `global.`/`us.` and any application profiles).
  # Invoking via an inference profile requires BOTH the profile ARN AND the
  # underlying foundation-model ARNs, so both are listed. This is far tighter
  # than Resource="*", which also granted every other Bedrock resource (agents,
  # knowledge bases, guardrails, custom models, provisioned throughput). Tighten
  # further to e.g. foundation-model/anthropic.* if your posture requires it.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
      ]
      Resource = [
        "arn:${local.partition}:bedrock:*::foundation-model/*",
        "arn:${local.partition}:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
      ]
    }]
  })
}

# The ServiceAccount the LiteLLM Deployment runs as. Always present so the
# Deployment's `serviceAccountName: litellm` resolves; the IRSA annotation is
# added only when Bedrock is enabled (empty annotations map otherwise → behaves
# like the default SA with no AWS access).
resource "kubernetes_service_account" "litellm" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "litellm"
    namespace = "ai-platform"
    annotations = local.enable_bedrock ? {
      "eks.amazonaws.com/role-arn" = aws_iam_role.litellm_bedrock[0].arn
    } : {}
    labels = {
      "app.kubernetes.io/part-of" = "ai-platform"
    }
  }

  depends_on = [kubernetes_namespace.ai_platform]
}

# Region for the Bedrock provider, read by LiteLLM via configMapKeyRef
# (AWS_REGION → os.environ/AWS_REGION in config.yaml). Kept out of git so the
# region isn't hardcoded in a committed manifest.
resource "kubernetes_config_map" "litellm_env" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "litellm-env"
    namespace = "ai-platform"
  }

  data = merge(
    {
      AWS_REGION = local.region
      # Bedrock runtime endpoint, read by LiteLLM via os.environ/ in config.yaml
      # (LiteLLM's Bedrock path signs its own requests — it does NOT read boto3's
      # native endpoint env vars — so we must feed it a concrete URL). Built from
      # the partition's DNS suffix so the committed litellm.yaml stays
      # partition-agnostic: aws -> amazonaws.com (the SDK default), aws-eusc ->
      # amazonaws.eu (the sovereign host; the .com default doesn't resolve there).
      # Set unconditionally (like AWS_REGION): LiteLLM reads it through os.environ,
      # so it must never be absent on a Terraform install.
      AWS_ENDPOINT_URL_BEDROCK_RUNTIME = "https://bedrock-runtime.${local.region}.${data.aws_partition.current.dns_suffix}"
    },
    # ESC (aws-eusc) only: force the sovereign-partition STS endpoint so IRSA
    # (AssumeRoleWithWebIdentity) validates against the ESC OIDC provider. An SDK
    # that doesn't know the aws-eusc partition otherwise resolves STS to the
    # commercial endpoint and fails with InvalidIdentityToken. The endpoint is
    # sts.<region>.<dns_suffix>; for aws-eusc dns_suffix is amazonaws.eu (same
    # domain family as the EKS/ECR/OIDC endpoints) — confirmed via
    # `aws sts get-caller-identity --debug`. Commercial (partition "aws") omits
    # the key and uses the SDK's default (correct) STS resolution.
    {
      for k, v in {
        AWS_ENDPOINT_URL_STS = local.partition == "aws-eusc" ? "https://sts.${local.region}.${data.aws_partition.current.dns_suffix}" : ""
      } : k => v if v != ""
    },
  )

  depends_on = [kubernetes_namespace.ai_platform]
}
