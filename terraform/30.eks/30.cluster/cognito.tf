################################################################################
# Identity plane — Amazon Cognito (SSO for the platform UIs + per-user cost)
#
# Ships a working OIDC identity provider out of the box. SSO works immediately
# via `platformctl tunnel` (Cognito permits http://localhost callback URLs);
# set var.sso_public_urls once a public HTTPS front (CloudFront) is deployed to
# add the public callbacks. Enterprise: federate an external IdP into this pool
# (not created here — an opt-in add-on). Identity Center stays required only for
# ArgoCD SSO.
#
# Everything here is gated on `local.enable_sso` and is inert until `apply`.
# The app manifests consume the `sso-secrets`/`oauth2-proxy-secrets` created
# below via OPTIONAL secret refs, so a cluster without SSO enabled still boots.
################################################################################

locals {
  enable_sso = local.capabilities.gitops && var.enable_sso

  # Per-app OIDC callback/logout paths + the local tunnel port. localhost
  # callbacks make SSO usable out of the box via `platformctl tunnel`.
  sso_apps = {
    "open-webui"   = { port = 8080, callback = "/oauth/oidc/callback", logout = "/" }
    "litellm"      = { port = 4000, callback = "/sso/callback", logout = "/" }
    "langfuse"     = { port = 3000, callback = "/api/auth/callback/custom", logout = "/" }
    "oauth2-proxy" = { port = 9090, callback = "/oauth2/callback", logout = "/" }
  }

  # Each SSO app's CloudFront edge key (the dashboard UI is fronted by the
  # oauth2-proxy gate, so its edge origin is keyed "dashboard").
  sso_edge_key = {
    "open-webui"   = "open-webui"
    "litellm"      = "litellm"
    "langfuse"     = "langfuse"
    "oauth2-proxy" = "dashboard"
  }

  # Public base URL per app: the CloudFront edge URL when that edge is enabled
  # (auto-wired, no manual step), else an explicit var.sso_public_urls entry
  # (e.g. an internet-facing ALB behind your own domain), else none.
  sso_public_url = {
    for app, cfg in local.sso_apps : app =>
    try(local.edge_public_urls[local.sso_edge_key[app]], try(var.sso_public_urls[app], ""))
  }

  # callback/logout URL lists = localhost tunnel + optional public URL.
  sso_callbacks = {
    for app, cfg in local.sso_apps : app => compact([
      "http://localhost:${cfg.port}${cfg.callback}",
      local.sso_public_url[app] != "" ? "${local.sso_public_url[app]}${cfg.callback}" : "",
    ])
  }
  sso_logouts = {
    for app, cfg in local.sso_apps : app => compact([
      "http://localhost:${cfg.port}${cfg.logout}",
      local.sso_public_url[app] != "" ? "${local.sso_public_url[app]}${cfg.logout}" : "",
    ])
  }

  sso_groups = ["ai-platform-admins", "ai-platform-developers", "ai-platform-users"]

  # LiteLLM role per group. LiteLLM reads this from userInfo via
  # GENERIC_USER_ROLE_ATTRIBUTE=custom:role, because Cognito does NOT return
  # cognito:groups on the userInfo endpoint (only in the ID/access token).
  sso_litellm_roles = {
    "ai-platform-admins"     = "proxy_admin"
    "ai-platform-developers" = "internal_user"
    "ai-platform-users"      = "internal_user_view_only"
  }

  # Seed users (one per role). Passwords are generated + surfaced as outputs.
  # example.com (RFC 2606 documentation domain) is used deliberately: LiteLLM's
  # SSO email validator rejects reserved/special-use domains like .local, so the
  # seed emails must use a normal domain. No email is ever sent (admin-created
  # users with a permanent password + email_verified=true).
  sso_seed_users = {
    "admin@example.com"     = "ai-platform-admins"
    "developer@example.com" = "ai-platform-developers"
    "user@example.com"      = "ai-platform-users"
  }

  cognito_domain_prefix = local.enable_sso ? "${local.cluster_name}-${random_string.cognito_domain_suffix[0].result}" : ""
  cognito_issuer_url    = local.enable_sso ? "https://cognito-idp.${local.region}.amazonaws.com/${aws_cognito_user_pool.platform[0].id}" : ""
  cognito_hosted_ui_url = local.enable_sso ? "https://${aws_cognito_user_pool_domain.platform[0].domain}.auth.${local.region}.amazoncognito.com" : ""
}

# Cognito prefix domains are globally unique across all AWS accounts — a random
# suffix avoids collisions between forkers using the same resources_prefix.
resource "random_string" "cognito_domain_suffix" {
  count   = local.enable_sso ? 1 : 0
  length  = 6
  special = false
  upper   = false
}

resource "aws_cognito_user_pool" "platform" {
  count = local.enable_sso ? 1 : 0

  name                     = "${local.cluster_name}-ai-platform"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

  # Seed users are admin-created; forkers add more via the console/CLI.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  # Custom attribute carrying the LiteLLM role. Unlike cognito:groups, custom
  # attributes ARE returned by the userInfo endpoint, which is where LiteLLM's
  # generic SSO reads user attributes from.
  schema {
    name                     = "role"
    attribute_data_type      = "String"
    mutable                  = true
    developer_only_attribute = false
    required                 = false
    string_attribute_constraints {
      min_length = 0
      max_length = 64
    }
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  tags = local.tags
}

resource "aws_cognito_user_pool_domain" "platform" {
  count        = local.enable_sso ? 1 : 0
  domain       = local.cognito_domain_prefix
  user_pool_id = aws_cognito_user_pool.platform[0].id
}

resource "aws_cognito_user_group" "groups" {
  for_each = local.enable_sso ? toset(local.sso_groups) : toset([])

  name         = each.value
  user_pool_id = aws_cognito_user_pool.platform[0].id
  description  = "AI platform role group: ${each.value}"
}

resource "aws_cognito_user_pool_client" "apps" {
  for_each = local.enable_sso ? local.sso_apps : {}

  name         = "${local.cluster_name}-${each.key}"
  user_pool_id = aws_cognito_user_pool.platform[0].id

  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = local.sso_callbacks[each.key]
  logout_urls   = local.sso_logouts[each.key]

  # SRP (Secure Remote Password) + refresh only. ALLOW_USER_PASSWORD_AUTH is
  # deliberately omitted: it transmits the plaintext password to Cognito and
  # enables scripted credential-stuffing. The hosted-UI authorization-code flow
  # these apps use relies on SRP, so dropping it doesn't affect normal sign-in.
  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # Read attributes exposed to the app (returned by userInfo). custom:role feeds
  # LiteLLM's GENERIC_USER_ROLE_ATTRIBUTE.
  read_attributes = ["email", "email_verified", "custom:role"]
}

# Seed-user passwords (permanent, generated). Cognito-safe symbol set.
resource "random_password" "sso_seed_users" {
  for_each = local.enable_sso ? local.sso_seed_users : {}

  length           = 20
  min_lower        = 2
  min_upper        = 2
  min_numeric      = 2
  min_special      = 2
  override_special = "!@#$%^&*()-_=+"
}

resource "aws_cognito_user" "seed" {
  for_each = local.enable_sso ? local.sso_seed_users : {}

  user_pool_id = aws_cognito_user_pool.platform[0].id
  username     = each.key
  password     = random_password.sso_seed_users[each.key].result

  attributes = {
    email          = each.key
    email_verified = "true"
    "custom:role"  = local.sso_litellm_roles[each.value]
  }
}

resource "aws_cognito_user_in_group" "seed" {
  for_each = local.enable_sso ? local.sso_seed_users : {}

  user_pool_id = aws_cognito_user_pool.platform[0].id
  group_name   = aws_cognito_user_group.groups[each.value].name
  username     = aws_cognito_user.seed[each.key].username
}

# Cookie secret for oauth2-proxy (must be 16/24/32 bytes for AES).
resource "random_password" "oauth2_proxy_cookie" {
  count   = local.enable_sso ? 1 : 0
  length  = 32
  special = false
}

# SSO config consumed by Open WebUI / LiteLLM / Langfuse (via OPTIONAL refs, so
# clusters without SSO still boot). Client id/secret per app + issuer + domain.
resource "kubernetes_secret" "sso_secrets" {
  count = local.enable_sso ? 1 : 0

  metadata {
    name      = "sso-secrets"
    namespace = "ai-platform"
  }

  data = merge(
    {
      "issuer"            = local.cognito_issuer_url
      "hosted-ui-domain"  = local.cognito_hosted_ui_url
      "openid-config-url" = "${local.cognito_issuer_url}/.well-known/openid-configuration"
      "authorize-url"     = "${local.cognito_hosted_ui_url}/oauth2/authorize"
      "token-url"         = "${local.cognito_hosted_ui_url}/oauth2/token"
      "userinfo-url"      = "${local.cognito_hosted_ui_url}/oauth2/userInfo"
      "admin-group"       = "ai-platform-admins"
      # Seed admin's user_id (LiteLLM keys SSO users by email) -> PROXY_ADMIN_ID.
      "admin-user-id" = one([for e, g in local.sso_seed_users : e if g == "ai-platform-admins"])
      # Public base URL per UI. Defaults to the localhost tunnel URL (so SSO
      # works out of the box via `platformctl tunnel`); overridden by the
      # CloudFront edge (auto) or an explicit var.sso_public_urls entry.
      "open-webui-public-url" = local.sso_public_url["open-webui"] != "" ? local.sso_public_url["open-webui"] : "http://localhost:8080"
      "litellm-public-url"    = local.sso_public_url["litellm"] != "" ? local.sso_public_url["litellm"] : "http://localhost:4000"
      "langfuse-public-url"   = local.sso_public_url["langfuse"] != "" ? local.sso_public_url["langfuse"] : "http://localhost:3000"
      # Explicit OAuth callback for Open WebUI (it derives the wrong http:8080
      # URL from the request behind the TLS-terminating edge). Public edge
      # callback when set, else the localhost tunnel callback.
      "open-webui-oidc-redirect-uri" = "${local.sso_public_url["open-webui"] != "" ? local.sso_public_url["open-webui"] : "http://localhost:8080"}${local.sso_apps["open-webui"].callback}"
      # OIDC end-session endpoint for Open WebUI. Cognito's /logout is NOT
      # standard OIDC RP-logout: it ignores id_token_hint and requires
      # client_id + logout_uri, and it publishes no end_session_endpoint, so Open
      # WebUI's default sign-out hits a "Client does not exist" Cognito error.
      # OPENID_END_SESSION_ENDPOINT points sign-out at a correctly-formed Cognito
      # logout URL (logout_uri is a registered LogoutURL: the edge URL when set,
      # else the localhost tunnel).
      "open-webui-oidc-end-session-endpoint" = "${local.cognito_hosted_ui_url}/logout?client_id=${aws_cognito_user_pool_client.apps["open-webui"].id}&logout_uri=${urlencode("${local.sso_public_url["open-webui"] != "" ? local.sso_public_url["open-webui"] : "http://localhost:8080"}${local.sso_apps["open-webui"].logout}")}"
    },
    { for app in keys(local.sso_apps) : "${app}-client-id" => aws_cognito_user_pool_client.apps[app].id },
    { for app in keys(local.sso_apps) : "${app}-client-secret" => aws_cognito_user_pool_client.apps[app].client_secret },
  )

  depends_on = [kubernetes_namespace.ai_platform]
}

# Dedicated secret for the dashboard's oauth2-proxy (client + cookie secret).
resource "kubernetes_secret" "oauth2_proxy_secrets" {
  count = local.enable_sso ? 1 : 0

  metadata {
    name      = "oauth2-proxy-secrets"
    namespace = "ai-platform"
  }

  data = {
    "client-id"     = aws_cognito_user_pool_client.apps["oauth2-proxy"].id
    "client-secret" = aws_cognito_user_pool_client.apps["oauth2-proxy"].client_secret
    "cookie-secret" = random_password.oauth2_proxy_cookie[0].result
    "issuer"        = local.cognito_issuer_url
    # Edge-aware, consistent with the app-client callback_urls above: the
    # CloudFront "dashboard" URL when the edge is on, else var.sso_public_urls,
    # else the localhost tunnel.
    "redirect-url" = "${local.sso_public_url["oauth2-proxy"] != "" ? local.sso_public_url["oauth2-proxy"] : "http://localhost:9090"}/oauth2/callback"
  }

  depends_on = [kubernetes_namespace.ai_platform]
}

################################################################################
# Outputs
################################################################################
output "sso_hosted_ui_url" {
  description = "Cognito Hosted UI (login page) base URL."
  value       = local.enable_sso ? local.cognito_hosted_ui_url : null
}

output "sso_issuer_url" {
  description = "Cognito OIDC issuer URL (OPENID_PROVIDER_URL / discovery base)."
  value       = local.enable_sso ? local.cognito_issuer_url : null
}

output "sso_user_pool_id" {
  description = "Cognito user pool ID."
  value       = local.enable_sso ? aws_cognito_user_pool.platform[0].id : null
}

output "sso_seed_user_passwords" {
  description = "Generated passwords for the SSO seed users (email -> password). Sign in at the Hosted UI or the platform UIs."
  value       = { for email in keys(local.sso_seed_users) : email => try(random_password.sso_seed_users[email].result, null) }
  sensitive   = true
}
