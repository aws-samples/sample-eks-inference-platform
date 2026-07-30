locals {
  tags = merge(
    var.tags,
    {
      "Environment" : terraform.workspace
      "provisioned-by" : "aws-solutions-library-samples/guidance-for-automated-provisioning-of-application-ready-amazon-eks-clusters"
    }

  )
  name = "${var.shared_config.resources_prefix}-${terraform.workspace}"
  # The below IAM roles represent the default Kubernetes user-facing roles as documented in https://kubernetes.io/docs/reference/access-authn-authz/rbac/#user-facing-roles
  #  and as supported by Amazon EKS Cluster Access Management.
  # Each role's in-cluster permissions are granted by the EKS access-policy
  # association in 30.cluster/main.tf (access_entries), not by attached IAM
  # policies — these roles only need a trust policy to be assumable.
  iam_roles = {
    EKSClusterAdmin = { role_name = "EKSClusterAdmin" },
    EKSAdmin        = { role_name = "EKSAdmin" },
    EKSEdit         = { role_name = "EKSEdit" },
    EKSView         = { role_name = "EKSView" },
  }
}
