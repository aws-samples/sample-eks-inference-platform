################################################################################
# metrics-server (metrics.k8s.io) — EKS managed addon
#
# Provides actual node/pod CPU + memory usage for `kubectl top`, the Horizontal
# Pod Autoscaler, and the cluster-dashboard's Infrastructure "actual usage"
# view. Installed as an AWS-managed EKS addon so every cluster has metrics.k8s.io
# at provisioning time — no manual `kubectl apply`, no dependency on ArgoCD being
# up. `OVERWRITE` lets the addon cleanly adopt a pre-existing install.
#
# Tolerates the CriticalAddonsOnly taint so it schedules on the system managed
# node group even before Karpenter provisions workload nodes on a fresh cluster.
################################################################################
data "aws_eks_addon_version" "metrics_server" {
  addon_name         = "metrics-server"
  kubernetes_version = module.eks.cluster_version
  most_recent        = true
}

resource "aws_eks_addon" "metrics_server" {
  cluster_name  = module.eks.cluster_name
  addon_name    = "metrics-server"
  addon_version = data.aws_eks_addon_version.metrics_server.version

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  configuration_values = jsonencode({
    tolerations = local.critical_addons_tolerations.tolerations
  })

  tags = local.tags
}
