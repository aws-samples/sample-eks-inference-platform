variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default = {

  }
}

variable "shared_config" {
  description = "Shared configuration across all modules/folders"
  type        = map(any)
  default     = {}
}

variable "trusted_principal_arns" {
  description = "List of IAM principal ARNs allowed to assume the EKS roles. Defaults to the current caller."
  type        = list(string)
  default     = []
}

variable "region" {
  description = "AWS region for this environment. The AWS provider resolves it from AWS_REGION (which platformctl pins from this value); declared here so it is a first-class, documented input rather than an undeclared tfvars key."
  type        = string
  default     = null
}
