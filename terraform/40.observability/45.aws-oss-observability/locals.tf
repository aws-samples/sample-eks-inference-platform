locals {

  name = "${var.shared_config.resources_prefix}-${terraform.workspace}"

  region         = data.aws_region.current.name
  sso_region     = try(var.observability_configuration.aws_oss_tooling_config.sso_region, local.region)
  tfstate_region = try(var.tfstate_region, local.region)

  eks_cluster_endpoint = data.aws_eks_cluster.this.endpoint
  eks_cluster_name     = data.terraform_remote_state.eks.outputs.cluster_name

  grafana_workspace_name                   = local.name
  grafana_workspace_description            = join("", ["Amazon Managed Grafana workspace for ${local.grafana_workspace_name}"])
  grafana_workspace_api_expiration_days    = 30
  grafana_workspace_api_expiration_seconds = 60 * 60 * 24 * local.grafana_workspace_api_expiration_days
  # Rotate at 80% of the key's TTL so a fresh key is always provisioned BEFORE
  # the old one expires. Rotation is driven by time_rotating.this, which forces
  # replacement of aws_grafana_workspace_api_key.this (see grafana_operator.tf).
  # Must be strictly less than grafana_workspace_api_expiration_days.
  grafana_workspace_api_rotation_days = floor(local.grafana_workspace_api_expiration_days * 0.8)

  critical_addons_tolerations = {
    tolerations = [
      {
        key      = "CriticalAddonsOnly",
        operator = "Exists",
        effect   = "NoSchedule"
      }
    ]
  }

  tags = merge(
    var.tags,
    {
      "Environment" : terraform.workspace
      "provisioned-by" : "aws-solutions-library-samples/guidance-for-automated-provisioning-of-application-ready-amazon-eks-clusters"
    }
  )
}
