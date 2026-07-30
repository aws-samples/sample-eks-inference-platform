variable "tfstate_region" {
  description = "region where the terraform state is stored"
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}

variable "observability_configuration" {
  description = "observability configuration variable"
  type = object({
    aws_oss_tooling        = optional(bool, true)  // AMP & AMG
    aws_native_tooling     = optional(bool, false) // CW
    aws_oss_tooling_config = optional(map(any), {})
  })
  default = {
    aws_oss_tooling        = false
    aws_native_tooling     = false
    aws_oss_tooling_config = {}
  }
}

variable "region" {
  description = "AWS region for this environment. The AWS provider resolves it from AWS_REGION (which platformctl pins from this value); declared here so it is a first-class, documented input rather than an undeclared tfvars key."
  type        = string
  default     = null
}
