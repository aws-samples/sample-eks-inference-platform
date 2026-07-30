# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, we ask that you notify
AWS/Amazon Security via our
[vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/)
or directly via email to aws-security@amazon.com. Please do **not** create a public
GitHub issue.

## Deploying this project securely

This is a **sample**. It provisions real, billable infrastructure. By default the
platform UIs are private (internal load balancer, reachable via `./platformctl
tunnel` or an opt-in CloudFront edge). Before you deploy, review and harden the
following for your environment:

- **Internal ALB by default.** The platform UIs (Open WebUI, LiteLLM, Langfuse,
  dashboard) sit behind an **internal** Application Load Balancer with no public IP —
  unreachable from the internet. Reach them with `./platformctl tunnel` (an **AWS
  SSM port-forwarding session** through an EKS node to the service ClusterIPs — note
  this deliberately bypasses the ALB and its inbound-CIDR allowlist, so treat local
  tunnel access as privileged) or expose them publicly via the opt-in **CloudFront
  VPC-origin edge** (`./platformctl edge cloudfront`, built in Terraform), which
  fronts the private ALB with HTTPS + SSO. If you instead switch the ALB to
  `internet-facing` (all four ingress `scheme` values), you **must** set the
  `inbound-cidrs` allowlist to your own IP ranges — **never leave it open to
  `0.0.0.0/0`**.
- **Dashboard remediation approvals require a verified admin.** Approving (or
  dismissing/deleting) a Platform Health Agent remediation spawns a cluster-mutating
  Job, so the dashboard backend cryptographically verifies the Cognito OIDC token
  (signature, issuer, audience, and `ai-platform-admins` group membership) on those
  endpoints, plus a CSRF header — independent of network path. Because the raw
  `tunnel` path carries no verified identity, **approvals are only available through
  the SSO-authenticated ALB/CloudFront UI**, not the tunnel. Disabling this
  (`DASHBOARD_AUTH_REQUIRED=false`) is only safe on a trusted network with SSO off.
- **Per-user budgets & rate limits.** LiteLLM enforces a default per-user spend
  budget and rpm/tpm throttle on the Open WebUI chat path (via the forwarded identity)
  and caps self-served API keys. Review the defaults in
  `platform/services/litellm/litellm.yaml` and adjust for your environment.
- **Secrets.** Provide model tokens, API keys, and other secrets via the mechanisms
  described in the README (tfvars / Kubernetes secrets), never by committing them.
  `*.tfvars` (except `example*.tfvars`) and state files are git-ignored — keep it that
  way.
- **Cost.** GPU nodes and the EKS control plane incur significant cost. Use the
  teardown steps in the README to remove everything when finished.
- **Least privilege.** Review the IAM roles and Kubernetes RBAC the platform creates
  and scope them down for production use.

These are starting points, not a complete production hardening guide. Review against
your own security and compliance requirements before any non-experimental use.
