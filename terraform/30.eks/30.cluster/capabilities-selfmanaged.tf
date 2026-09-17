################################################################################
# Self-managed ArgoCD + KRO (Helm)
#
# Used when cluster_config.capabilities.eks_capabilities = false — i.e. on
# partitions/regions where AWS EKS Managed Capabilities are unavailable (notably
# the AWS European Sovereign Cloud, eusc-de-east-1, where CreateCapability does
# not exist). These Helm releases stand in for aws_eks_capability.argocd / .kro
# so the rest of the platform is unchanged: the ArgoCD bootstrap Application
# (kubectl_manifest.argocd_bootstrap), the local-cluster secret, the KRO
# ResourceGraphDefinitions, and the serving CRDs all work against a self-managed
# ArgoCD/KRO exactly as they would against the managed capabilities.
#
# When eks_capabilities = true these are NOT created (count = 0) — the managed
# aws_eks_capability.* resources provide ArgoCD/KRO instead. This installs them
# as part of `./platformctl up` (Terraform), replacing the previous manual
# `helm install argocd ...` / `helm install kro ...` steps.
#
################################################################################

resource "helm_release" "argocd_selfmanaged" {
  count = local.capabilities.gitops && !local.use_managed_capabilities ? 1 : 0

  name             = "argocd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  namespace        = "argocd"
  create_namespace = true

  # Wait for the release to be fully deployed (including the Application/
  # ApplicationSet CRDs) so kubernetes_secret.argocd_cluster and
  # kubectl_manifest.argocd_bootstrap below apply against a ready ArgoCD.
  wait    = true
  timeout = 900

  depends_on = [module.eks]
}

resource "helm_release" "kro_selfmanaged" {
  count = local.capabilities.kro && !local.use_managed_capabilities ? 1 : 0

  name       = "kro"
  repository = "oci://registry.k8s.io/kro/charts"
  chart      = "kro"
  version    = "0.9.4"
  namespace  = "kro-system"

  create_namespace = true
  wait             = true
  timeout          = 600

  depends_on = [module.eks]
}

################################################################################
# Self-managed ACK controllers (Helm) — when eks_capabilities = false and ack = true
#
# Unlike ArgoCD/KRO (one install each), ACK is a SET of per-service controllers
# (one chart per AWS service), and each controller needs its own IRSA role. So
# the self-managed path is driven by var.ack_service_controllers: a map of
# service name -> IAM policy ARNs. This mirrors what the managed ACK capability
# would provide, gated on the same `ack` flag. Empty map (the default) installs
# nothing — so `ack = true` on a self-managed cluster is inert until you list the
# controllers you want. Ignored entirely when eks_capabilities = true.
################################################################################
locals {
  ack_selfmanaged = (local.capabilities.ack && !local.use_managed_capabilities) ? var.ack_service_controllers : {}

  # Flatten {svc => [arn, ...]} into {"svc|arn" => {svc, arn}} so each policy
  # attachment is its own for_each instance.
  ack_policy_attachments = merge([
    for svc, arns in local.ack_selfmanaged : {
      for arn in arns : "${svc}|${arn}" => { svc = svc, arn = arn }
    }
  ]...)
}

# One IRSA role per ACK controller, bound to its ack-<svc>-controller SA in
# the ack-system namespace via the cluster OIDC provider.
resource "aws_iam_role" "ack_selfmanaged" {
  for_each = local.ack_selfmanaged

  name = "${local.cluster_name}-ack-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${module.eks.oidc_provider}:sub" = "system:serviceaccount:ack-system:ack-${each.key}-controller"
          "${module.eks.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "ack_selfmanaged" {
  for_each   = local.ack_policy_attachments
  role       = aws_iam_role.ack_selfmanaged[each.value.svc].name
  policy_arn = each.value.arn
}

resource "helm_release" "ack_selfmanaged" {
  for_each = local.ack_selfmanaged

  name             = "ack-${each.key}"
  repository       = "oci://public.ecr.aws/aws-controllers-k8s"
  chart            = "${each.key}-chart"
  namespace        = "ack-system"
  create_namespace = true

  values = [yamlencode({
    aws = { region = local.region }
    serviceAccount = {
      name = "ack-${each.key}-controller"
      annotations = {
        "eks.amazonaws.com/role-arn" = aws_iam_role.ack_selfmanaged[each.key].arn
      }
    }
  })]

  depends_on = [module.eks, aws_iam_role_policy_attachment.ack_selfmanaged]
}