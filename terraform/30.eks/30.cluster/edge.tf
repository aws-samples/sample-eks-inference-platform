################################################################################
# CloudFront edge (opt-in) — public HTTPS for the platform UIs via CloudFront
# VPC origins to the platform's INTERNAL ALB.
#
# WHY TERRAFORM (not ACK): the EKS-managed ACK CloudFront controller lacks
# cloudfront:CreateVpcOrigin and can't be extended (AWS caps it with a managed
# session policy), so VPC origins can't be created via ACK on a managed cluster.
# Terraform has full IAM and is the right owner of AWS resources (tenet 11).
#
# TWO-PHASE: the ALB is created by the in-cluster load-balancer controller only
# AFTER `up` (ArgoCD syncs the ingresses), so this is a POST-UP step — enable it
# with `./platformctl edge cloudfront`, which flips enable_cloudfront_edge and
# runs a targeted apply once the ALB exists. The ALB is discovered by tag; no
# ARN plumbing. The ALB stays `internal`; CloudFront reaches it privately over
# the VPC origin (the shared ALB SG already allows the VPC CIDR — see
# platform/config/ingress.yaml inbound-cidrs).
#
# Cache is disabled (managed CachingDisabled) with all viewer headers/cookies
# forwarded (managed AllViewer) — required for dynamic, authenticated apps.
# Viewer cert = the free *.cloudfront.net default (HTTPS, no domain/ACM). The
# resulting domains feed the Cognito callback URLs directly (see cognito.tf) —
# no manual sso_public_urls step.
################################################################################

locals {
  enable_cloudfront_edge = local.capabilities.gitops && var.enable_cloudfront_edge

  # UI -> ALB listener port (the shared `ai-platform` ingress group; one VPC
  # origin per port). Keys match the Cognito app-client keys in cognito.tf.
  edge_ports = {
    "open-webui" = 8080
    "litellm"    = 4000
    "langfuse"   = 3000
    "dashboard"  = 9090
  }

  # AWS-managed CloudFront policies (stable IDs across accounts).
  cf_caching_disabled_policy_id = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # CachingDisabled
  cf_all_viewer_orp_id          = "216adef6-5c7f-47e4-b989-5492eafa07d3" # AllViewer

  # Public HTTPS URL per UI once the edge exists (empty when disabled). Consumed
  # by cognito.tf to set the OIDC callback/logout URLs without a manual step.
  edge_public_urls = local.enable_cloudfront_edge ? {
    for k, d in aws_cloudfront_distribution.edge : k => "https://${d.domain_name}"
  } : {}
}

# Discover the shared internal ALB (tagged by the AWS Load Balancer Controller
# for the `ai-platform` ingress group). Resolvable only after `up`.
data "aws_lb" "platform" {
  count = local.enable_cloudfront_edge ? 1 : 0

  tags = {
    "elbv2.k8s.aws/cluster" = local.cluster_name
    "ingress.k8s.aws/stack" = "ai-platform"
  }
}

# One VPC origin per ALB listener port — CloudFront's private path to the ALB.
resource "aws_cloudfront_vpc_origin" "edge" {
  for_each = local.enable_cloudfront_edge ? local.edge_ports : {}

  vpc_origin_endpoint_config {
    name                   = "${local.cluster_name}-${each.key}"
    arn                    = data.aws_lb.platform[0].arn
    http_port              = each.value
    https_port             = 443
    origin_protocol_policy = "http-only"

    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }

  tags = local.tags
}

# Access logs for the edge distributions (CKV_AWS_86). CloudFront *standard*
# logging delivers to S3 through its log-delivery group, which requires the
# bucket to have ACLs enabled (BucketOwnerPreferred) and to use SSE-S3 —
# CloudFront standard logging does not support SSE-KMS buckets. Public access
# stays fully blocked; the log-delivery grant is account-scoped, not public.
resource "aws_s3_bucket" "edge_logs" {
  #checkov:skip=CKV_AWS_18:This IS the access-log bucket; enabling access logging on it would be circular.
  #checkov:skip=CKV_AWS_144:Cross-region replication is unwarranted for regenerable CloudFront access logs.
  #checkov:skip=CKV_AWS_145:CloudFront standard logging cannot deliver to SSE-KMS buckets; SSE-S3 (AES256) is required.
  #checkov:skip=CKV2_AWS_62:Access-log sink; S3 event notifications are unnecessary.
  count         = local.enable_cloudfront_edge ? 1 : 0
  bucket_prefix = "${local.cluster_name}-cf-edge-logs-"
  force_destroy = true # logs are regenerable; safe to destroy with the cluster

  tags = merge(local.tags, { Purpose = "cloudfront-edge-access-logs" })
}

resource "aws_s3_bucket_ownership_controls" "edge_logs" {
  #checkov:skip=CKV2_AWS_65:ACLs must stay enabled (BucketOwnerPreferred) for CloudFront standard logging's log-delivery group to write.
  count  = local.enable_cloudfront_edge ? 1 : 0
  bucket = aws_s3_bucket.edge_logs[0].id
  rule {
    # ACLs ON — required by CloudFront standard logging's log-delivery group.
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_public_access_block" "edge_logs" {
  count                   = local.enable_cloudfront_edge ? 1 : 0
  bucket                  = aws_s3_bucket.edge_logs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "edge_logs" {
  count  = local.enable_cloudfront_edge ? 1 : 0
  bucket = aws_s3_bucket.edge_logs[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "edge_logs" {
  count  = local.enable_cloudfront_edge ? 1 : 0
  bucket = aws_s3_bucket.edge_logs[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "edge_logs" {
  count  = local.enable_cloudfront_edge ? 1 : 0
  bucket = aws_s3_bucket.edge_logs[0].id

  rule {
    id     = "expire-edge-logs"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
    expiration {
      days = 90
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
  }
}

# One distribution per UI, fronting its VPC origin. No caching (auth'd apps).
resource "aws_cloudfront_distribution" "edge" {
  # CKV_AWS_174: A minimum TLS version (>= 1.2) can only be enforced with a
  # custom ACM certificate. This edge intentionally uses the free
  # *.cloudfront.net default certificate (no domain required), whose TLS
  # security policy is managed by AWS and cannot be raised via
  # minimum_protocol_version. Operators who must pin TLS 1.2+ use
  # `./platformctl edge domain` (internet-facing ALB + their own ACM cert).
  #checkov:skip=CKV_AWS_174:Default *.cloudfront.net cert TLS policy is AWS-managed; use `edge domain` mode (ACM cert) to pin TLS1.2+.
  # CKV_AWS_305: no default_root_object by design — each distribution is a
  # reverse proxy to a single application on the ALB, and the application owns
  # "/". Setting a root object would make CloudFront rewrite "/" to a static
  # object the origin does not serve, breaking the app's landing page.
  #checkov:skip=CKV_AWS_305:Reverse proxy to an app origin; the app serves "/", a default root object would break it.

  for_each = local.enable_cloudfront_edge ? local.edge_ports : {}

  # Make sure the log bucket's ownership (ACLs enabled) is in place before
  # CloudFront's log-delivery group needs to write to it.
  depends_on = [aws_s3_bucket_ownership_controls.edge_logs]

  enabled     = true
  comment     = "ai-platform ${each.key}"
  price_class = "PriceClass_100"

  origin {
    origin_id   = "alb"
    domain_name = data.aws_lb.platform[0].dns_name

    vpc_origin_config {
      vpc_origin_id = aws_cloudfront_vpc_origin.edge[each.key].id
    }
  }

  default_cache_behavior {
    target_origin_id         = "alb"
    viewer_protocol_policy   = "redirect-to-https"
    cache_policy_id          = local.cf_caching_disabled_policy_id
    origin_request_policy_id = local.cf_all_viewer_orp_id
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"]
    cached_methods           = ["GET", "HEAD"]

    # Upgrade app-generated redirects (e.g. http://<domain>:8080/... from apps
    # that build Location from the request, since TLS terminates here and the ALB
    # listener is HTTP) to https://<domain>/... so OAuth/UI redirects don't break.
    function_association {
      event_type   = "viewer-response"
      function_arn = aws_cloudfront_function.upgrade_redirects[0].arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  logging_config {
    bucket          = aws_s3_bucket.edge_logs[0].bucket_domain_name
    include_cookies = false
    prefix          = "${each.key}/"
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = local.tags
}

# CloudFront Function (viewer-response): rewrite Location headers of the form
# http://<host>:<port>/path -> https://<host>/path. Apps like Open WebUI and
# LiteLLM build redirect URLs from the request; behind this TLS-terminating edge
# (ALB listener is HTTP) that yields http + the ALB port, which the browser
# can't reach on CloudFront. This upgrades them at the edge — generic, no app or
# ALB changes. Pure string ops (no regex) for CloudFront Functions' JS runtime.
resource "aws_cloudfront_function" "upgrade_redirects" {
  count   = local.enable_cloudfront_edge ? 1 : 0
  name    = "${local.cluster_name}-edge-upgrade-redirects"
  runtime = "cloudfront-js-2.0"
  comment = "Upgrade http://host:port Location headers to https://host"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var response = event.response;
      var loc = response.headers && response.headers.location;
      if (loc && loc.value && loc.value.indexOf('http://') === 0) {
        var rest = loc.value.substring(7);
        var slash = rest.indexOf('/');
        var hostport = slash === -1 ? rest : rest.substring(0, slash);
        var path = slash === -1 ? '' : rest.substring(slash);
        var colon = hostport.indexOf(':');
        var host = colon === -1 ? hostport : hostport.substring(0, colon);
        loc.value = 'https://' + host + path;
      }
      return response;
    }
  EOT
}

output "edge_cloudfront_urls" {
  description = "Public HTTPS URL per UI once the CloudFront edge is enabled (empty otherwise). Reach the UIs at these; SSO callbacks are wired automatically."
  value       = local.edge_public_urls
}

# The CloudFront VPC-origins service SG — one per VPC, created by CloudFront when
# the first VPC origin is made. Discovered by its well-known tag so it's generic
# across forks (no hardcoded SG id).
data "aws_security_group" "cloudfront_vpcorigins" {
  count = local.enable_cloudfront_edge ? 1 : 0

  vpc_id = data.terraform_remote_state.vpc.outputs.vpc_id
  tags   = { "aws.cloudfront.vpcorigin" = "enabled" }

  depends_on = [aws_cloudfront_vpc_origin.edge]
}

# THE rule that makes the edge reachable: allow the CloudFront VPC origin to hit
# the ALB on each UI port. VPC-origin traffic is matched by SG-reference (not the
# VPC CIDR), and this lives on our own frontend SG (alb-security-group.tf) so the
# AWS LB Controller can't strip it. Removed automatically when the edge is off.
resource "aws_vpc_security_group_ingress_rule" "edge_from_cloudfront" {
  for_each = local.enable_cloudfront_edge ? local.edge_ports : {}

  security_group_id            = aws_security_group.alb_frontend.id
  referenced_security_group_id = data.aws_security_group.cloudfront_vpcorigins[0].id
  from_port                    = each.value
  to_port                      = each.value
  ip_protocol                  = "tcp"
  description                  = "CloudFront VPC origin to ${each.key}"
}
