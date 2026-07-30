output "configure_kubectl" {
  description = "Configure kubectl"
  value       = "aws eks --region ${local.region} update-kubeconfig --name ${module.eks.cluster_name}"
}

output "cluster_name" {
  description = "The EKS cluster name"
  value       = module.eks.cluster_name
}

output "argocd_url" {
  description = "ArgoCD UI URL (login with Identity Center)"
  value       = local.capabilities.gitops ? aws_eks_capability.argocd[0].configuration[0].argo_cd[0].server_url : null
}

output "next_steps" {
  description = "Post-deploy steps"
  value       = <<-EOT

    1. Configure kubectl:
       aws eks --region ${local.region} update-kubeconfig --name ${module.eks.cluster_name}

    2. Verify ArgoCD synced the platform (Terraform already bootstrapped it):
       kubectl get applicationsets -n argocd
       kubectl get applications -n argocd

    3. Create HuggingFace token (for gated models):
       kubectl create secret generic hf-token -n inference --from-literal=token=hf_YOUR_TOKEN

    4. Get LiteLLM master key:
       kubectl get secret litellm-secrets -n ai-platform -o jsonpath='{.data.master-key}' | base64 -d

    5. Langfuse tracing is live on first boot — no key setup needed.
       The sign-in URL is auto-set to the ALB hostname (langfuse-nextauth-url
       CronJob). Print it once the ingress is up:
         echo "http://$(kubectl get ingress ai-platform-langfuse -n ai-platform \
           -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'):3000"
         user: ${var.langfuse_init_user_email}
         pass: terraform output -raw langfuse_admin_password

    6. Try the frontier model with zero GPUs (Bedrock Claude Opus 4.8):
       requires Bedrock model access enabled in-account; see ./platformctl status --check
  EOT
}

output "langfuse_admin_email" {
  description = "Email of the Langfuse admin user created on first boot"
  value       = local.capabilities.gitops ? var.langfuse_init_user_email : null
}

output "langfuse_admin_password" {
  description = "Password for the Langfuse admin user (generated). Sign in at the langfuse_nextauth_url."
  value       = local.capabilities.gitops ? random_password.langfuse_init_user[0].result : null
  sensitive   = true
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "cluster_certificate_authority_data" {
  value = module.eks.cluster_certificate_authority_data
}

output "kubernetes_version" {
  description = "The EKS Cluster version"
  value       = module.eks.cluster_version
}

output "oidc_provider_arn" {
  description = "The OIDC Provider ARN"
  value       = module.eks.oidc_provider_arn
}

output "control_plane_subnet_ids" {
  description = "The Control Plane Subnet IDs"
  value       = local.control_plane_subnet_ids
}