data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_iam_session_context" "current" {
  # This data source provides information on the IAM source role of an STS assumed role
  # For non-role ARNs, this data source simply passes the ARN through issuer ARN
  # Ref https://github.com/terraform-aws-modules/terraform-aws-eks/issues/2327#issuecomment-1355581682
  # Ref https://github.com/hashicorp/terraform-provider-aws/issues/28381
  arn = data.aws_caller_identity.current.arn
}


################################################################################
# EKS Cluster
################################################################################
# Fail-closed guard: if the public EKS API endpoint is enabled it must be scoped
# to a non-open CIDR allowlist. Evaluated at plan time (not `terraform validate`,
# so CI's schema check is unaffected), it stops an apply that would expose the
# control plane to 0.0.0.0/0. Private-only clusters (private_eks_cluster = true)
# skip the requirement.
resource "terraform_data" "validate_public_endpoint" {
  lifecycle {
    precondition {
      condition = try(!var.cluster_config.private_eks_cluster, false) == false || (
        length(var.cluster_endpoint_public_access_cidrs) > 0 &&
        !contains(var.cluster_endpoint_public_access_cidrs, "0.0.0.0/0")
      )
      error_message = "The EKS public API endpoint is enabled (cluster_config.private_eks_cluster = false) but is not scoped. Set cluster_endpoint_public_access_cidrs to your operator IP/CIDR range(s) (never 0.0.0.0/0), or set private_eks_cluster = true for a VPC-private endpoint."
    }
  }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "= 21.1.5"

  name               = local.cluster_name
  kubernetes_version = local.cluster_version
  # When the public API endpoint is enabled (private_eks_cluster = false, the
  # laptop-provisioning default) it MUST be scoped to an operator CIDR allowlist.
  # The terraform_data.validate_public_endpoint precondition below fail-closes if
  # this is empty or open, so a fork can't leave the API server reachable from
  # 0.0.0.0/0 (the old behaviour). Ignored for private-only clusters.
  endpoint_public_access       = try(!var.cluster_config.private_eks_cluster, false)
  endpoint_public_access_cidrs = var.cluster_endpoint_public_access_cidrs

  create_iam_role          = try(var.cluster_config.create_iam_role, true)
  iam_role_arn             = try(var.cluster_config.cluster_iam_role_arn, null)
  iam_role_use_name_prefix = false

  node_iam_role_use_name_prefix = false

  enabled_log_types = ["audit", "api", "authenticator", "controllerManager", "scheduler"]

  vpc_id                   = data.terraform_remote_state.vpc.outputs.vpc_id
  subnet_ids               = local.private_subnet_ids
  control_plane_subnet_ids = local.control_plane_subnet_ids

  # Combine root account, current user/role and additinoal roles to be able to access the cluster KMS key - required for terraform updates
  kms_key_administrators = distinct(concat([
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"],
    var.kms_key_admin_roles,
    [data.aws_iam_session_context.current.issuer_arn]
  ))

  enable_cluster_creator_admin_permissions = true
  authentication_mode                      = try(var.cluster_config.authentication_mode, "API")

  #bootstrap_self_managed_addons = "false"

  compute_config = local.eks_auto_mode ? { enabled = local.eks_auto_mode } : null

  # We're using EKS module to only provision EKS managed addons
  # The reason for that is to use the `before_compute` parameter which allows for the addon to be deployed before a compute is available for it to run
  addons = merge(
    local.capabilities.networking ? {
      vpc-cni = {
        # Specify the VPC CNI addon should be deployed before compute to ensure
        # the addon is configured before data plane compute resources are created
        before_compute = true
        most_recent    = true # To ensure access to the latest settings provided
        preserve       = false
        configuration_values = jsonencode({
          # Enforce Kubernetes NetworkPolicy. Without this the VPC CNI IGNORES
          # every NetworkPolicy in the cluster, so the team isolation and
          # platform-db policies would be decorative — teams could reach each
          # other and the platform Postgres regardless. Auto Mode enforces via
          # the NodeClass networkPolicy field instead (auto-mode/default.yaml).
          enableNetworkPolicy = "true"
          env = {
            ENABLE_PREFIX_DELEGATION = "false"
            WARM_ENI_TARGET          = "0"
            MINIMUM_IP_TARGET        = "10"
            WARM_IP_TARGET           = "5"
          }
        })
      }
    } : {},
    local.capabilities.kube_proxy ? {
      kube-proxy = {
        before_compute = true
        most_recent    = true
        preserve       = false
      }
    } : {},
    local.capabilities.coredns ? {
      coredns = {
        resolve_conflicts_on_create = "OVERWRITE"
        resolve_conflicts_on_update = "PRESERVE"
        preserve                    = false
        most_recent                 = true
        configuration_values = jsonencode(
          {
            replicaCount : 2,
            tolerations : [local.critical_addons_tolerations.tolerations[0]]
          }
        )
      }
    } : {},
    local.capabilities.identity ? {
      eks-pod-identity-agent = {
        most_recent = true
        preserve    = false
      }
    } : {},
    local.capabilities.blockstorage ? {
      aws-ebs-csi-driver = {
        service_account_role_arn = module.ebs_csi_driver_irsa[0].iam_role_arn
        preserve                 = false
        configuration_values = jsonencode({
          sidecars = {
            snapshotter = {
              forceEnable = false
            }
          }
        })
      }
    } : {}
  )

  access_entries = {
    EKSClusterAdmin = {
      kubernetes_groups = []
      principal_arn     = data.terraform_remote_state.iam.outputs.iam_roles_map["EKSClusterAdmin"]

      policy_associations = {
        single = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            type = "cluster"
          }
        }
      }
    }
    EKSAdmin = {
      kubernetes_groups = []
      principal_arn     = data.terraform_remote_state.iam.outputs.iam_roles_map["EKSAdmin"]

      policy_associations = {
        single = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminPolicy"
          access_scope = {
            type = "cluster"
          }
        }
      }
    }
    EKSEdit = {
      kubernetes_groups = []
      principal_arn     = data.terraform_remote_state.iam.outputs.iam_roles_map["EKSEdit"]
      policy_associations = {
        single = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"
          access_scope = {
            namespaces = ["default"]
            type       = "namespace"
          }
        }
      }
    }
    EKSView = {
      kubernetes_groups = []
      principal_arn     = data.terraform_remote_state.iam.outputs.iam_roles_map["EKSView"]

      policy_associations = {
        single = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"
          access_scope = {
            namespaces = ["default"]
            type       = "namespace"
          }
        }
      }
    }

  }

  # Fargate profiles use the cluster primary security group so these are not utilized
  create_security_group      = false
  create_node_security_group = false

  # managed node group for base EKS addons such as Karpenter
  eks_managed_node_groups = {
    "${local.cluster_name}-criticaladdons" = {
      create                   = local.create_mng_system
      iam_role_use_name_prefix = false
      subnet_ids               = data.terraform_remote_state.vpc.outputs.private_subnet_ids
      max_size                 = 8
      desired_size             = 2
      min_size                 = 2

      instance_types = ["m6g.large"]
      ami_type       = "AL2023_ARM_64_STANDARD"
      iam_role_additional_policies = {
        SSM = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
      }

      labels = {
        "role" : "system"
      }

      taints = {
        critical_addons = {
          key    = "CriticalAddonsOnly"
          effect = "NO_SCHEDULE"
        }
      }
    }
  }
  tags = local.tags
}

resource "null_resource" "update_kubeconfig" {
  depends_on = [module.eks]
  triggers = {
    cluster_name = module.eks.cluster_name
    region       = local.region
  }
  provisioner "local-exec" {
    command = "aws eks --region ${self.triggers.region} update-kubeconfig --name ${self.triggers.cluster_name}"
  }
}

################################################################################
# EBS CSI Driver
################################################################################
module "ebs_csi_driver_irsa" {
  count   = local.capabilities.blockstorage ? 1 : 0
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.43"

  role_name = "${local.cluster_name}-ebs-csi"

  role_policy_arns = {
    policy = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
  }

  oidc_providers = {
    cluster = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:ebs-csi-controller-sa"]
    }
  }
}

################################################################################
# EKS Auto Mode Node role access entry
################################################################################
resource "aws_eks_access_entry" "automode_node" {
  count         = local.eks_auto_mode ? 1 : 0
  cluster_name  = module.eks.cluster_name
  principal_arn = module.eks.node_iam_role_arn
  type          = "EC2"
}

resource "aws_eks_access_policy_association" "automode_node" {
  count        = local.eks_auto_mode ? 1 : 0
  cluster_name = module.eks.cluster_name
  access_scope {
    type = "cluster"
  }
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAutoNodePolicy"
  principal_arn = module.eks.node_iam_role_arn
}

################################################################################
# EKS Auto Mode default NodePools & NodeClass
################################################################################
data "kubectl_path_documents" "automode_manifests" {
  count   = local.eks_auto_mode ? 1 : 0
  pattern = "${path.module}/auto-mode/*.yaml"
  vars = {
    role                      = module.eks.node_iam_role_name
    cluster_name              = local.cluster_name
    cluster_security_group_id = module.eks.cluster_primary_security_group_id
    environment               = terraform.workspace
  }
  depends_on = [
    module.eks
  ]
}

# Count-stabilization workaround (kubectl provider issue #58). Twin of the
# karpenter_manifests_dummy in karpenter.tf (see the full rationale there): the
# real kubectl_path_documents above uses apply-time values, so this dummy
# renders the SAME auto-mode files with empty vars to get a plan-time-known
# document count for the kubectl_manifest `count`. Intentionally kept inline
# (not modularized) to avoid changing resource addresses on the live cluster.
# https://github.com/gavinbunney/terraform-provider-kubectl/issues/58
data "kubectl_path_documents" "automode_manifests_dummy" {
  count   = local.eks_auto_mode ? 1 : 0
  pattern = "${path.module}/auto-mode/*.yaml"
  vars = {
    role                      = ""
    cluster_name              = ""
    cluster_security_group_id = ""
    environment               = terraform.workspace
  }
}

resource "kubectl_manifest" "automode_manifests" {
  count     = local.eks_auto_mode ? length(data.kubectl_path_documents.automode_manifests_dummy[0].documents) : 0
  yaml_body = element(data.kubectl_path_documents.automode_manifests[0].documents, count.index)
}

################################################################################
# Storage Classes
################################################################################
resource "kubernetes_annotations" "gp2" {
  count       = local.capabilities.blockstorage || local.eks_auto_mode ? 1 : 0
  api_version = "storage.k8s.io/v1"
  kind        = "StorageClass"
  force       = "true"
  metadata {
    name = "gp2"
  }
  annotations = {
    "storageclass.kubernetes.io/is-default-class" = "false"
  }
  depends_on = [
    module.eks
  ]
}

resource "kubernetes_storage_class_v1" "gp3" {
  count = local.capabilities.blockstorage ? 1 : 0
  metadata {
    name = "gp3"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true" # make gp3 the default storage class
    }
  }
  storage_provisioner    = "ebs.csi.aws.com"
  allow_volume_expansion = true
  reclaim_policy         = "Delete"
  volume_binding_mode    = "WaitForFirstConsumer"
  parameters = {
    encrypted = true
    fsType    = "ext4"
    type      = "gp3"
  }
  depends_on = [
    module.eks
  ]
}

################################################################################
# EKS Auto Mode Storage Class
################################################################################
resource "kubernetes_storage_class_v1" "automode" {
  count = local.eks_auto_mode ? 1 : 0
  metadata {
    name = "auto-ebs-sc"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }
  storage_provisioner = "ebs.csi.eks.amazonaws.com"
  volume_binding_mode = "WaitForFirstConsumer"
  parameters = {
    encrypted = true
    type      = "gp3"
  }
  depends_on = [
    module.eks
  ]
}

################################################################################
# EKS Auto Mode Ingress
################################################################################
resource "kubectl_manifest" "automode_ingressclass_params" {
  count = local.eks_auto_mode ? 1 : 0
  # scheme: internal — the secure default, matching the non-Auto-Mode ingresses
  # (platform/config/ingress.yaml) and the whole edge design (private ALB reached
  # via the CloudFront VPC-origin edge or the SSM tunnel). As the DEFAULT
  # IngressClass this governs any Ingress that doesn't name a class, so an
  # internet-facing default would silently expose every such Ingress to the
  # public internet. Expose intentionally instead (own class / annotation).
  yaml_body = <<YAML
apiVersion: eks.amazonaws.com/v1
kind: IngressClassParams
metadata:
  name: auto-alb
spec:
  scheme: internal
YAML
  depends_on = [
    module.eks
  ]
}

resource "kubernetes_ingress_class_v1" "automode" {
  count = local.eks_auto_mode ? 1 : 0
  metadata {
    name = "auto-alb"
    annotations = {
      "ingressclass.kubernetes.io/is-default-class" = "true"
    }
  }
  spec {
    controller = "eks.amazonaws.com/alb"
    parameters {
      api_group = "eks.amazonaws.com"
      kind      = "IngressClassParams"
      name      = "auto-alb"
    }
  }
  depends_on = [
    kubectl_manifest.automode_ingressclass_params
  ]
}
