################################################################################
# Langfuse tracing on first boot — headless initialization
#
# Today's friction: deploy → log into Langfuse UI → create project + API keys →
# create the langfuse-litellm-keys secret → restart LiteLLM. This makes the
# whole dance automatic.
#
# Terraform generates a deterministic Langfuse project key pair (pk/sk) plus an
# admin user, and wires them two ways:
#   1. langfuse-init secret → consumed by the Langfuse chart via additionalEnv
#      (LANGFUSE_INIT_*), so the org/project/keys/user exist on first boot
#      (Langfuse headless initialization).
#   2. langfuse-litellm-keys secret (already referenced by the LiteLLM
#      Deployment) → the SAME pk/sk, so LiteLLM's Langfuse callback authenticates
#      against the project that was just created.
#
# Result: the first model call is traced — no UI, no Job, no restart.
# Keys live only in Kubernetes Secrets (Terraform-generated), never in git.
################################################################################

# Project API key pair. The prefixes (pk-lf-/sk-lf-) match Langfuse's real-world
# key convention so SDK-side prefix checks pass; the random tail is the secret.
resource "random_password" "langfuse_init_public_key" {
  count   = local.capabilities.gitops ? 1 : 0
  length  = 36
  special = false
}

resource "random_password" "langfuse_init_secret_key" {
  count   = local.capabilities.gitops ? 1 : 0
  length  = 36
  special = false
}

# Admin user password for the Langfuse UI (first login). Surfaced as a Terraform
# output so the operator can sign in; email is the configurable login.
resource "random_password" "langfuse_init_user" {
  count   = local.capabilities.gitops ? 1 : 0
  length  = 24
  special = false
}

locals {
  langfuse_public_key = local.capabilities.gitops ? "pk-lf-${random_password.langfuse_init_public_key[0].result}" : ""
  langfuse_secret_key = local.capabilities.gitops ? "sk-lf-${random_password.langfuse_init_secret_key[0].result}" : ""

  # Stable, single-tenant identifiers — one org, one project for the turnkey
  # platform. Experts can add more in the Langfuse UI later.
  langfuse_org_id     = "ai-platform"
  langfuse_project_id = "ai-platform"
}

# (1) Headless-init values consumed by the Langfuse chart via additionalEnv.
#     A Secret (not ConfigMap) because it carries the project keys + admin
#     password. NEXTAUTH_URL is included so the browser-facing URL is
#     Terraform-controlled (var.langfuse_nextauth_url) instead of a manual
#     post-install edit to the git-tracked helm values.
resource "kubernetes_secret" "langfuse_init" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "langfuse-init"
    namespace = "ai-platform"
  }

  data = {
    LANGFUSE_INIT_ORG_ID             = local.langfuse_org_id
    LANGFUSE_INIT_ORG_NAME           = "AI Platform"
    LANGFUSE_INIT_PROJECT_ID         = local.langfuse_project_id
    LANGFUSE_INIT_PROJECT_NAME       = "AI Platform"
    LANGFUSE_INIT_PROJECT_PUBLIC_KEY = local.langfuse_public_key
    LANGFUSE_INIT_PROJECT_SECRET_KEY = local.langfuse_secret_key
    LANGFUSE_INIT_USER_EMAIL         = var.langfuse_init_user_email
    LANGFUSE_INIT_USER_NAME          = "Platform Admin"
    LANGFUSE_INIT_USER_PASSWORD      = random_password.langfuse_init_user[0].result
    NEXTAUTH_URL                     = var.langfuse_nextauth_url
  }

  depends_on = [kubernetes_namespace.ai_platform]
}

# (2) Same pk/sk that LiteLLM's Langfuse callback authenticates with. This is
#     the secret the LiteLLM Deployment already references (langfuse-litellm-keys,
#     marked optional). Terraform now fills it in automatically.
resource "kubernetes_secret" "langfuse_litellm_keys" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "langfuse-litellm-keys"
    namespace = "ai-platform"
  }

  data = {
    LANGFUSE_PUBLIC_KEY = local.langfuse_public_key
    LANGFUSE_SECRET_KEY = local.langfuse_secret_key
  }

  depends_on = [kubernetes_namespace.ai_platform]
}
