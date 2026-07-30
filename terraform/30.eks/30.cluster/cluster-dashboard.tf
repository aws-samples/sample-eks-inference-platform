################################################################################
# Cluster dashboard — Quick Links ConfigMap
#
# The dashboard shows a "Quick Links" panel to the platform's web UIs. The
# ALB-fronted services (Open WebUI / LiteLLM / Langfuse) share one ingress
# hostname and are discovered by the dashboard backend at runtime, so they need
# no Terraform input. ArgoCD is an EKS-managed capability with its own endpoint
# (server_url) that can't be derived from the ALB — so we surface it here.
#
# Consumed by platform/services/cluster-dashboard/manifests.yaml via optional
# configMapKeyRefs: ARGOCD_URL (the dashboard hides the ArgoCD link if unset)
# and CLUSTER_NAME (the overview header; region is auto-detected from nodes).
################################################################################
resource "kubernetes_config_map" "cluster_dashboard_links" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "cluster-dashboard-links"
    namespace = "ai-platform"
  }

  data = {
    argocdUrl   = try(aws_eks_capability.argocd[0].configuration[0].argo_cd[0].server_url, "")
    clusterName = local.cluster_name
    # Public UI URLs when the CloudFront edge is enabled (empty otherwise) — the
    # dashboard's Quick Links prefer these over the internal-ALB host:port, which
    # is not browser-reachable from outside the VPC.
    openWebuiUrl = try(local.edge_public_urls["open-webui"], "")
    litellmUrl   = try(local.edge_public_urls["litellm"], "")
    langfuseUrl  = try(local.edge_public_urls["langfuse"], "")
  }

  depends_on = [
    kubernetes_namespace.ai_platform,
    aws_eks_capability.argocd,
  ]
}


################################################################################
# Cluster dashboard — live node pricing (AWS Price List API via Pod Identity)
#
# The dashboard's Cost view estimates node $/hr. So the estimate works in any
# account/region without a hardcoded table, the backend queries the AWS Price
# List API (pricing:GetProducts) for the running instance types in the region,
# caching results and falling back to its shipped/ConfigMap prices if this
# access is absent.
#
# The dashboard's ServiceAccount is created by the GitOps manifests
# (platform/services/cluster-dashboard/manifests.yaml), NOT Terraform — so we
# grant AWS access via an EKS Pod Identity association (no SA annotation, no
# conflict with ArgoCD self-heal) rather than IRSA. Requires the
# eks-pod-identity-agent addon, installed by default on this cluster.
#
# Read-only: the Price List API returns global pricing data, not account
# resources, so Resource = "*" is the only valid scope.
################################################################################
resource "aws_iam_role" "dashboard_pricing" {
  count = local.capabilities.gitops ? 1 : 0
  name  = "${local.cluster_name}-dashboard-pricing"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy" "dashboard_pricing" {
  count = local.capabilities.gitops ? 1 : 0
  name  = "price-list-read"
  role  = aws_iam_role.dashboard_pricing[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "pricing:GetProducts",
        "pricing:DescribeServices",
        "pricing:GetAttributeValues",
      ]
      Resource = "*"
    }]
  })
}

resource "aws_eks_pod_identity_association" "dashboard_pricing" {
  count = local.capabilities.gitops ? 1 : 0

  cluster_name    = module.eks.cluster_name
  namespace       = "ai-platform"
  service_account = "cluster-dashboard"
  role_arn        = aws_iam_role.dashboard_pricing[0].arn

  tags = local.tags
}
