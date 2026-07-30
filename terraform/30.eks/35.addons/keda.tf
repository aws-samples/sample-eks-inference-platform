################################################################################
# KEDA — event-driven autoscaler for the llm-d serving tier
#
# Installed as a Helm release: the aws-ia/eks-blueprints-addons module (used in
# main.tf) has no KEDA add-on, so we install the upstream chart directly — the
# same pattern Karpenter uses. KEDA's built-in Prometheus scaler drives replica
# autoscaling on vLLM saturation signals (queue depth / KV-cache utilization)
# and wakes scaled-to-zero pools on an arrival signal from the always-on llm-d
# EPP. Karpenter then follows the replica count for GPU nodes (including to zero).
#
# Gated by the `autoscaling` capability (on by default) so a cluster can opt out
# and keep fixed replicas. KEDA is not needed at bootstrap (it acts on workloads
# that appear later), so it schedules on default Karpenter nodes — no need for
# the CriticalAddonsOnly toleration the bootstrap addons use.
################################################################################
resource "helm_release" "keda" {
  count = local.capabilities.autoscaling ? 1 : 0

  name             = "keda"
  namespace        = "keda"
  create_namespace = true
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = "2.16.1"

  # Let CRDs install/upgrade cleanly across chart bumps.
  values = [yamlencode({
    crds = { install = true }
  })]
}
