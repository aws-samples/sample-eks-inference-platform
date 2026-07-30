locals {
  cluster_name    = "${var.shared_config.resources_prefix}-${terraform.workspace}"
  region          = data.aws_region.current.id
  tfstate_region  = try(var.tfstate_region, local.region)
  cluster_version = var.cluster_config.kubernetes_version
  eks_auto_mode   = try(var.cluster_config.eks_auto_mode, false)

  # Single source of truth for the vLLM serving image version.
  # Surfaced to KRO via the platform-config ConfigMap (vllmImage); the
  # VLLMEndpoint RGD reads it via externalRef, falling back to its schema default.
  vllm_image_tag = "v0.24.0"
  vllm_image     = "vllm/vllm-openai:${local.vllm_image_tag}"

  # HuggingFace model-weights cache.
  # S3 bucket is populated automatically by the vLLM
  # worker's sidecar after first successful load (automatic). The initContainer
  # in the serving-tier RGDs (VLLMEndpoint/LLMDEndpoint/LLMDDisaggEndpoint) syncs
  # from here on pod startup — falling back to a live HF download on cache miss.
  model_cache_bucket            = "${local.cluster_name}-model-cache"
  inference_worker_sa_namespace = "inference"
  inference_worker_sa_name      = "inference-worker"

  private_subnet_ids       = data.terraform_remote_state.vpc.outputs.private_subnet_ids
  control_plane_subnet_ids = try(var.cluster_config.use_intra_subnets, true) ? data.terraform_remote_state.vpc.outputs.intra_subnet_ids : local.private_subnet_ids

  capabilities = {
    kube_proxy   = try(var.cluster_config.capabilities.kube_proxy, !local.eks_auto_mode, true)
    networking   = try(var.cluster_config.capabilities.networking, !local.eks_auto_mode, true)
    coredns      = try(var.cluster_config.capabilities.coredns, !local.eks_auto_mode, true)
    identity     = try(var.cluster_config.capabilities.identity, !local.eks_auto_mode, true)
    autoscaling  = try(var.cluster_config.capabilities.autoscaling, !local.eks_auto_mode, true)
    blockstorage = try(var.cluster_config.capabilities.blockstorage, !local.eks_auto_mode, true)
    # EKS Managed Capabilities (AWS-managed, not self-managed)
    gitops = try(var.cluster_config.capabilities.gitops, false)
    kro    = try(var.cluster_config.capabilities.kro, false)
    ack    = try(var.cluster_config.capabilities.ack, false)
  }

  create_mng_system = try(var.cluster_config.create_mng_system, !local.eks_auto_mode, true)

  critical_addons_tolerations = {
    tolerations = [
      {
        key      = "CriticalAddonsOnly",
        operator = "Exists",
        effect   = "NoSchedule"
      }
    ]
  }

  tags = merge(
    var.tags,
    {
      "Environment" : terraform.workspace
      "provisioned-by" : "aws-solutions-library-samples/guidance-for-automated-provisioning-of-application-ready-amazon-eks-clusters"
    }
  )
}
