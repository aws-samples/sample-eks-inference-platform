output "external_secrets_addon_output" {
  description = "external-secrets addon output values"
  value       = try(var.observability_configuration.aws_oss_tooling, false) ? module.eks_blueprints_addons.external_secrets : null
}
