



data "aws_caller_identity" "current" {}

locals {
  trusted_principals = length(var.trusted_principal_arns) > 0 ? var.trusted_principal_arns : [data.aws_caller_identity.current.arn]
}

# Create IAM roles and attach policies
resource "aws_iam_role" "iam_roles" {
  for_each = local.iam_roles

  name = "${local.name}-${each.value.role_name}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Action = "sts:AssumeRole",
        Effect = "Allow",
        Principal = {
          AWS = local.trusted_principals
        },
      },
    ],
  })
}

