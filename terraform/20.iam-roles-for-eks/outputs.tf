
# Output IAM roles map with names and ARNs
output "iam_roles_map" {
  value = {
    for role_name, role_config in aws_iam_role.iam_roles : role_name => role_config.arn
  }
}
