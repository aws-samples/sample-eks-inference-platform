data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

# AWS Load Balancer Controller v3.x IAM supplement. The blueprints module's
# built-in LBC policy predates v3 and lacks ec2:DescribeRouteTables, which v3
# calls during subnet discovery (read-only; scoping mirrors the module's other
# ec2:Describe* statements). Diffed against the upstream v3.4.3
# docs/install/iam_policy.json — this is the only missing action.
data "aws_iam_policy_document" "lbc_v3_supplement" {
  statement {
    sid       = "LBCv3DescribeRouteTables"
    effect    = "Allow"
    actions   = ["ec2:DescribeRouteTables"]
    resources = ["*"]
  }
}

module "eks_blueprints_addons" {
  source  = "aws-ia/eks-blueprints-addons/aws"
  version = "~> 1.21.0"

  cluster_name      = data.terraform_remote_state.eks.outputs.cluster_name
  cluster_endpoint  = data.terraform_remote_state.eks.outputs.cluster_endpoint
  cluster_version   = data.terraform_remote_state.eks.outputs.kubernetes_version
  oidc_provider_arn = data.terraform_remote_state.eks.outputs.oidc_provider_arn

  create_kubernetes_resources = true

  # common addons deployed with EKS Blueprints Addons
  enable_aws_load_balancer_controller = local.capabilities.loadbalancing
  aws_load_balancer_controller = {
    # Pin the LB controller chart. Since LBC v3.0.0 the chart version aligns
    # with the app version (v2.x used chart 1.x). The pinned blueprints module
    # defaults to chart 1.7.1 (app v2.7.1), which is far behind upstream.
    # NOTE for LIVE-cluster upgrades from v2.x: Helm does not upgrade CRDs —
    # apply the latest CRDs first:
    #   kubectl apply -k "github.com/aws/eks-charts/stable/aws-load-balancer-controller/crds?ref=master"
    # Fresh installs (new forker clusters) get current CRDs automatically.
    chart_version = "3.4.3"
    # LBC v3 needs ec2:DescribeRouteTables (subnet discovery), which the
    # pinned module's built-in policy predates. Merged in via the module's
    # source_policy_documents passthrough.
    source_policy_documents = [data.aws_iam_policy_document.lbc_v3_supplement.json]
    set = [
      {
        name  = "vpcId"
        value = data.terraform_remote_state.vpc.outputs.vpc_id
      }
    ]
    values = [yamlencode(local.critical_addons_tolerations)]
  }


  # external-secrets is being used AMG for grafana auth
  enable_external_secrets = try(var.observability_configuration.aws_oss_tooling, false)
  external_secrets = {
    values = [
      yamlencode({
        tolerations = [local.critical_addons_tolerations.tolerations[0]]
        webhook = {
          tolerations = [local.critical_addons_tolerations.tolerations[0]]
        }
        certController = {
          tolerations = [local.critical_addons_tolerations.tolerations[0]]
        }
      })
    ]
  }

  # cert-manager as a dependency for ADOT addon
  enable_cert_manager = try(
    var.observability_configuration.aws_oss_tooling
    && var.observability_configuration.aws_oss_tooling_config.enable_adot_collector,
  false)
  cert_manager = {
    values = [
      yamlencode({
        tolerations = [local.critical_addons_tolerations.tolerations[0]]
        webhook = {
          tolerations = [local.critical_addons_tolerations.tolerations[0]]
        }
        cainjector = {
          tolerations = [local.critical_addons_tolerations.tolerations[0]]
        }
      })
    ]
  }

  # FluentBit 
  enable_aws_for_fluentbit = try(
    var.observability_configuration.aws_oss_tooling
    && !var.observability_configuration.aws_oss_tooling_config.enable_adot_collector
  , false)
  aws_for_fluentbit = {
    values = [
      yamlencode({ "tolerations" : [{ "operator" : "Exists" }] })
    ]
  }
  aws_for_fluentbit_cw_log_group = {
    name            = "/aws/eks/${data.terraform_remote_state.eks.outputs.cluster_name}/aws-fluentbit-logs"
    use_name_prefix = false
    create          = true
  }
}
