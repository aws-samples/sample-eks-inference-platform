# VPC CIDR — the ALB frontend SG derives its rules from this automatically.
vpc_cidr = "10.10.0.0/16"

tags = {}

shared_config = {
  resources_prefix = "ai-platform"
}

cluster_config = {
  kubernetes_version  = "1.36"
  eks_auto_mode       = true # AWS manages compute, networking, storage
  private_eks_cluster = false
  create_mng_system   = false # Not needed — Auto Mode manages nodes

  capabilities = {
    kube_proxy    = false # Managed by Auto Mode
    networking    = false # Managed by Auto Mode
    coredns       = false # Managed by Auto Mode
    identity      = false # Managed by Auto Mode
    autoscaling   = false # Managed by Auto Mode (no Karpenter)
    blockstorage  = false # Managed by Auto Mode
    loadbalancing = false # Managed by Auto Mode

    # EKS Managed Capabilities (AWS-managed, run in AWS-owned infrastructure)
    gitops = true # ArgoCD — requires Identity Center (see capabilities_config)
    kro    = true # Kube Resource Orchestrator
    ack    = true # AWS Controllers for Kubernetes
  }

  # Required when gitops = true
  # See: https://docs.aws.amazon.com/eks/latest/userguide/argocd.html
  capabilities_config = {
    argocd_idc_instance_arn = "arn:aws:sso:::instance/ssoins-XXXXXXXXXX" # REPLACE
    argocd_idc_region       = "us-east-1"                                # REPLACE
    argocd_rbac_mappings = [
      {
        role = "ADMIN"
        identities = [
          { id = "REPLACE-WITH-SSO-USER-ID", type = "SSO_USER" }
        ]
      }
    ]
  }
}

observability_configuration = {
  aws_oss_tooling    = false
  aws_native_tooling = false
}

# ECR Pull-Through Cache (optional) — ~60% faster image pulls
# Set via environment variables, not in this file:
#   export TF_VAR_docker_hub_username="your-dockerhub-username"
#   export TF_VAR_docker_hub_access_token="dckr_pat_XXXXXXXXXX"

# GitOps repository — ArgoCD syncs the platform from this repo.
# When you FORK, also set the same URL/branch in argocd/bootstrap/platform.yaml
# ($repo/$rev) and argocd/bootstrap/workloads.yaml (repoURL) — ArgoCD generators
# can't read this tfvar. See those files' headers for the fork checklist.
gitops_repo_url = "https://github.com/YOUR-ORG/YOUR-REPO.git"
gitops_revision = "main"

# Amazon Bedrock (default: enabled) — exposes frontier models (e.g. Claude
# Sonnet 4.6) through LiteLLM with zero GPUs. Requires Bedrock model access
# enabled in-account for the target model (one-time AWS console toggle).
# enable_bedrock = true

# Langfuse first-boot init (tracing live on the first call — no manual setup).
# Default URL works with the SSM tunnel (./platformctl tunnel). For ALB access use
# e.g. http://k8s-aiplatform-<hash>.<region>.elb.amazonaws.com:3000, or
# https://langfuse.<your-domain> behind a domain + cert.
# langfuse_nextauth_url    = "http://localhost:3000"
# langfuse_init_user_email = "admin@ai-platform.local"
