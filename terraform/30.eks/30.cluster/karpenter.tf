# NOTE (ESC): ECR Public is a commercial us-east-1-only service, so
# aws_ecrpublic_authorization_token authenticated with aws-eusc credentials fails
# with InvalidClientTokenId. The Karpenter chart and controller image are pulled
# anonymously from public.ecr.aws over NAT egress instead (public.ecr.aws issues an
# anonymous bearer token; Helm and containerd perform that token flow automatically).
# To avoid internet egress entirely, mirror both into a private ECR repo in
# eusc-de-east-1 and repoint repository / controller.image below.

# Add the Karpenter discovery tag only to the cluster primary security group
# by default if using the eks module tags, it will tag all resources with this tag, which is not needed.
resource "aws_ec2_tag" "cluster_primary_security_group" {
  count       = local.capabilities.autoscaling ? 1 : 0
  resource_id = module.eks.cluster_primary_security_group_id
  key         = "karpenter.sh/discovery"
  value       = local.cluster_name
}

################################################################################
# Karpenter node IAM role (ESC override)
#
# The terraform-aws-modules/karpenter module builds the node role's trust
# principal as ec2.${data.aws_partition.current.dns_suffix}. In aws-eusc the
# partition dns_suffix is "amazonaws.eu", yielding "ec2.amazonaws.eu", which the
# ESC IAM service rejects ("Invalid principal in policy"). ESC uses the standard
# "ec2.amazonaws.com" principal (as module.eks's own managed node group role
# does). The module exposes no override for this principal, so we create the
# node role here with the correct principal and hand it to the module via
# create_node_iam_role = false + node_iam_role_arn.
################################################################################
data "aws_iam_policy_document" "karpenter_node_assume" {
  count = local.capabilities.autoscaling ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "karpenter_node" {
  count              = local.capabilities.autoscaling ? 1 : 0
  name               = "KarpenterNode-${module.eks.cluster_name}"
  assume_role_policy = data.aws_iam_policy_document.karpenter_node_assume[0].json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "karpenter_node" {
  for_each = local.capabilities.autoscaling ? merge(
    {
      worker = "arn:${local.partition}:iam::aws:policy/AmazonEKSWorkerNodePolicy"
      cni    = "arn:${local.partition}:iam::aws:policy/AmazonEKS_CNI_Policy"
      ecr    = "arn:${local.partition}:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
      ssm    = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
    },
    var.docker_hub_username != "" ? { ecrpull = aws_iam_policy.ecr_pull_through[0].arn } : {}
  ) : {}

  role       = aws_iam_role.karpenter_node[0].name
  policy_arn = each.value
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
  #   - ec2:DescribeCapacityReservations → required for
  #     capacityReservationSelectorTerms (ODCR / Capacity Block discovery for
  #     gpu_capacity_reservation_ids; ReservedCapacity gate is on by default
  #     since Karpenter 1.6). Harmless when no reservations are configured.
  # All are read-only; consistent with the module's AllowRegionalReadActions.
  iam_policy_statements = [
    {
      sid       = "AllowInstanceStatusAndPlacementGroupReads"
      effect    = "Allow"
      resources = ["*"]
      actions = [
        "ec2:DescribeInstanceStatus",
        "ec2:DescribePlacementGroups",
        "ec2:DescribeCapacityReservations",
        "iam:ListInstanceProfiles",
      ]
    }
  ]

  # Used to attach additional IAM policies to the Karpenter node IAM role.
  # ESC: the node role is created outside this module (see aws_iam_role.karpenter_node)
  # so its trust principal is ec2.amazonaws.com rather than the module's
  # ec2.${dns_suffix} = ec2.amazonaws.eu (rejected by ESC IAM). All node policies
  # — including AmazonSSMManagedInstanceCore and the optional ECR pull-through
  # import policy — are attached there via aws_iam_role_policy_attachment.karpenter_node.
  create_node_iam_role = false
  node_iam_role_arn    = one(aws_iam_role.karpenter_node[*].arn)

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

  namespace  = "kube-system"
  name       = "karpenter"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
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

locals {
  # --- GPU capacity reservations (ODCRs / Capacity Blocks) -------------------
  # Rendered as a complete YAML block for the gpu-inference EC2NodeClass when
  # reservations are configured, or an empty string (→ blank line) when not —
  # same pattern as gpu_snapshot_id_line. The placeholder sits at the NodeClass
  # spec indent (2 spaces); continuation lines carry their own indent.
  gpu_capacity_reservation_selector = length(var.gpu_capacity_reservation_ids) == 0 ? "" : join("\n", concat(
    ["capacityReservationSelectorTerms:"],
    [for id in var.gpu_capacity_reservation_ids : "    - id: ${id}"],
  ))
  # NodePool karpenter.sh/capacity-type values. "reserved" is only valid when
  # the NodeClass actually selects reservations (Karpenter rejects it
  # otherwise); when present, Karpenter prioritizes reserved capacity before
  # falling back to on-demand/spot.
  gpu_capacity_types = join(", ", [
    for t in concat(
      length(var.gpu_capacity_reservation_ids) > 0 ? ["reserved"] : [],
      ["spot", "on-demand"],
    ) : format("%q", t)
  ])

  # GPU data volume size — the baked image snapshot's size by default, or the
  # operator override for very large models (weights land on this volume via
  # the hf-cache emptyDir; Kimi-K3-class checkpoints need ~2 TiB).
  gpu_node_volume_gib = var.gpu_node_volume_size_gib > 0 ? var.gpu_node_volume_size_gib : local.snapshot_volume_gib
}

data "kubectl_path_documents" "karpenter_manifests" {
  count   = (local.capabilities.autoscaling || local.eks_auto_mode) ? 1 : 0
  pattern = "${path.module}/karpenter/*.yaml"
  vars = {
    role         = local.capabilities.autoscaling ? aws_iam_role.karpenter_node[0].name : "KarpenterNodeInstanceProfile-${local.cluster_name}"
    cluster_name = local.cluster_name
    environment  = terraform.workspace
    # Renders as a complete YAML line when set, or empty string when not.
    # This avoids passing an invalid empty snapshotID to Karpenter.
    # Source: explicit tfvar override > auto-discovered snapshot > disabled.
    gpu_snapshot_id_line = local.resolved_snapshot_id != "" ? "snapshotID: ${local.resolved_snapshot_id}" : ""
    # GPU data volume size — must be >= the baked snapshot's volume size.
    # Kept in sync with local.snapshot_volume_gib (overridable via
    # gpu_node_volume_size_gib for very large models).
    gpu_volume_size = local.gpu_node_volume_gib
    # Capacity Blocks / ODCRs for GPU inference nodes (empty = disabled).
    gpu_capacity_reservation_selector = local.gpu_capacity_reservation_selector
    gpu_capacity_types                = local.gpu_capacity_types
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
    gpu_volume_size      = local.gpu_node_volume_gib
    # Plan-time-known (pure function of tfvars) — same values as the real
    # block, but they only need to keep the document COUNT stable.
    gpu_capacity_reservation_selector = local.gpu_capacity_reservation_selector
    gpu_capacity_types                = local.gpu_capacity_types
  }
}

resource "kubectl_manifest" "karpenter_manifests" {
  count     = (local.capabilities.autoscaling || local.eks_auto_mode) ? length(data.kubectl_path_documents.karpenter_manifests_dummy[0].documents) : 0
  yaml_body = element(data.kubectl_path_documents.karpenter_manifests[0].documents, count.index)

  depends_on = [helm_release.karpenter]
}
