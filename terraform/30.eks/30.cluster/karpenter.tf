data "aws_ecrpublic_authorization_token" "token" {
  provider = aws.virginia
}

# Add the Karpenter discovery tag only to the cluster primary security group
# by default if using the eks module tags, it will tag all resources with this tag, which is not needed.
resource "aws_ec2_tag" "cluster_primary_security_group" {
  count       = local.capabilities.autoscaling ? 1 : 0
  resource_id = module.eks.cluster_primary_security_group_id
  key         = "karpenter.sh/discovery"
  value       = local.cluster_name
}

################################################################################
# Karpenter
################################################################################
module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "= 21.1.5"

  create = local.capabilities.autoscaling

  cluster_name = module.eks.cluster_name

  create_pod_identity_association = true

  # Additional controller permissions required by newer Karpenter versions that
  # the pinned module (v21.1.5) does not yet grant:
  #   - ec2:DescribeInstanceStatus  → required since Karpenter 1.12 for the
  #     interruption controller's EC2 instance-status health checks.
  #   - ec2:DescribePlacementGroups → required since Karpenter 1.11 for
  #     placement-group support (harmless when unused; future-proofs the role).
  #   - iam:ListInstanceProfiles    → required since Karpenter 1.7 for the
  #     instance-profile controller/garbage-collection (does not support
  #     resource-level scoping, so it must target "*").
  # All are read-only; consistent with the module's AllowRegionalReadActions.
  iam_policy_statements = [
    {
      sid       = "AllowInstanceStatusAndPlacementGroupReads"
      effect    = "Allow"
      resources = ["*"]
      actions = [
        "ec2:DescribeInstanceStatus",
        "ec2:DescribePlacementGroups",
        "iam:ListInstanceProfiles",
      ]
    }
  ]

  # Used to attach additional IAM policies to the Karpenter node IAM role.
  # The ECR pull-through import policy is attached only when the docker-hub
  # pull-through cache is enabled — GPU nodes pull the vLLM serving image from
  # the cache at runtime, and the FIRST pull must import the upstream image (403s with the
  # default read-only node policy otherwise → ImagePullBackOff).
  node_iam_role_additional_policies = merge(
    {
      AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    },
    var.docker_hub_username != "" ? {
      EcrPullThroughImport = aws_iam_policy.ecr_pull_through[0].arn
    } : {},
  )

  iam_role_name            = "KarpenterController-${module.eks.cluster_name}"
  iam_role_use_name_prefix = false

  node_iam_role_name            = "KarpenterNode-${module.eks.cluster_name}"
  node_iam_role_use_name_prefix = false

  tags = local.tags

  depends_on = [
    module.eks
  ]
}

################################################################################
# Karpenter Helm chart deployment
################################################################################
resource "helm_release" "karpenter" {
  count = local.capabilities.autoscaling ? 1 : 0

  namespace           = "kube-system"
  name                = "karpenter"
  repository          = "oci://public.ecr.aws/karpenter"
  repository_username = data.aws_ecrpublic_authorization_token.token.user_name
  repository_password = data.aws_ecrpublic_authorization_token.token.password
  chart               = "karpenter"
  # NOTE (CRDs): Helm does NOT upgrade CRDs bundled under the chart's `crds/`
  # directory on `helm upgrade` — it only installs them on first `helm install`.
  # Karpenter v1.14 moves the CapacityBuffer CRD to apiVersion v1beta1, so an
  # in-place upgrade of a LIVE cluster requires the CRDs to be applied first
  # (e.g. via the karpenter-crd chart / kubectl apply) BEFORE this release is
  # upgraded. This only affects future/new clusters here, where the CRDs are
  # installed fresh on the initial `helm install`, so no manual step is needed.
  # NOTE (DRA): Dynamic Resource Allocation is enabled by default in v1.14.
  # `IGNORE_DRA_REQUESTS` is the escape hatch to opt back out. It is harmless
  # for this cluster (no DRA-based scheduling in use), so left at the default.
  version = "1.14.0"
  wait    = false

  values = [
    yamlencode({
      tolerations = local.critical_addons_tolerations.tolerations,
      dnsPolicy : "Default",
      settings = {
        clusterName : module.eks.cluster_name
        clusterEndpoint : module.eks.cluster_endpoint
        interruptionQueue : module.karpenter.queue_name
        featureGates = {
          nodeOverlay = true
        }
      },
      controller = {
        resources = {
          requests = {
            cpu    = "1"
            memory = "1Gi"
          },
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }
      }
    })
  ]

  depends_on = [
    module.karpenter
  ]
}
################################################################################
# Karpenter default NodePool & NodeClass
# Create NodePools for both self-managed Karpenter and EKS Auto Mode (managed Karpenter)
################################################################################
data "kubectl_path_documents" "karpenter_manifests" {
  count   = (local.capabilities.autoscaling || local.eks_auto_mode) ? 1 : 0
  pattern = "${path.module}/karpenter/*.yaml"
  vars = {
    role         = local.capabilities.autoscaling ? module.karpenter.node_iam_role_name : "KarpenterNodeInstanceProfile-${local.cluster_name}"
    cluster_name = local.cluster_name
    environment  = terraform.workspace
    # Renders as a complete YAML line when set, or empty string when not.
    # This avoids passing an invalid empty snapshotID to Karpenter.
    # Source: explicit tfvar override > auto-discovered snapshot > disabled.
    gpu_snapshot_id_line = local.resolved_snapshot_id != "" ? "snapshotID: ${local.resolved_snapshot_id}" : ""
    # GPU data volume size — must be >= the baked snapshot's volume size.
    # Kept in sync with local.snapshot_volume_gib.
    gpu_volume_size = local.snapshot_volume_gib
  }
  depends_on = [
    module.eks
  ]
}

# Count-stabilization workaround (kubectl provider issue #58). The real
# kubectl_path_documents above interpolates values only known at apply
# (module.karpenter role, resolved snapshot ID, ...), so its `.documents`
# length is unknown at plan and can't drive the kubectl_manifest `count`. This
# "dummy" renders the SAME files with empty vars: the document COUNT is
# identical and plan-time-known, while the real content comes from the resource
# above. Deliberately NOT factored into a shared module with the auto-mode twin
# in main.tf — the call-sites differ in vars, and a module would change these
# resources' addresses, forcing destroy/recreate of the live Karpenter
# NodePools/NodeClasses. Fileset counting can't replace it (a file may hold
# multiple YAML documents). Revisit if the provider fixes unknown-count support.
# https://github.com/gavinbunney/terraform-provider-kubectl/issues/58
data "kubectl_path_documents" "karpenter_manifests_dummy" {
  count   = (local.capabilities.autoscaling || local.eks_auto_mode) ? 1 : 0
  pattern = "${path.module}/karpenter/*.yaml"
  vars = {
    role                 = ""
    cluster_name         = ""
    environment          = terraform.workspace
    gpu_snapshot_id_line = ""
    gpu_volume_size      = local.snapshot_volume_gib
  }
}

resource "kubectl_manifest" "karpenter_manifests" {
  count     = (local.capabilities.autoscaling || local.eks_auto_mode) ? length(data.kubectl_path_documents.karpenter_manifests_dummy[0].documents) : 0
  yaml_body = element(data.kubectl_path_documents.karpenter_manifests[0].documents, count.index)

  depends_on = [helm_release.karpenter]
}
