data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

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
