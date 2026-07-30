# CloudFront edge — public HTTPS for the platform UIs

The platform's ALB is **internal** by default (no public IP). To expose the UIs
publicly over HTTPS without a domain, run:

```bash
./platformctl edge cloudfront        # opt-in, billable; run AFTER `up`
./platformctl status                 # shows the *.cloudfront.net URLs
./platformctl edge tunnel            # turn it back off (destroys the distributions)
```

Terraform stands up, per UI (Open WebUI · LiteLLM · Langfuse · Dashboard), a
CloudFront **distribution** whose origin is a **VPC origin** pointing at the
private ALB. TLS terminates at CloudFront (free `*.cloudfront.net` cert); the
Cognito OIDC callbacks and the dashboard's "Quick Links" are rewired to the
CloudFront URLs automatically, and the affected apps are restarted so they pick
up the new URLs. It's all in `terraform/30.eks/30.cluster/edge.tf` +
`alb-security-group.tf` and toggled by `enable_cloudfront_edge`.

## Three non-obvious things the Terraform handles for you

These are the parts that make a hand-rolled "CloudFront in front of an internal
ALB" setup fail. If you fork and extend the edge, this is why the code looks the
way it does.

### 1. The ALB must allow the VPC origin by security-group *reference*, not CIDR

CloudFront VPC-origin traffic reaching the ALB is **not** matched by the VPC
CIDR — you must allow the CloudFront VPC-origins security group
(`CloudFront-VPCOrigins-Service-SG`, tagged `aws.cloudfront.vpcorigin=enabled`,
one per VPC) as the *source*. And the AWS Load Balancer Controller strips any
foreign rule from the SG it auto-creates from `inbound-cidrs`. So the platform
**owns** the ALB frontend SG (`ai-platform-alb-inbound`): the four ingresses
reference it via `alb.ingress.kubernetes.io/security-groups`, it allows the VPC
CIDR (in-VPC / tunnel) always, and `edge.tf` adds the CloudFront-SG allow-rule
when the edge is on. The controller still manages the backend SG, so ALB→pod
traffic is unaffected. **Symptom if missing: CloudFront returns 504.**

### 2. App redirects must be upgraded from `http://…:<port>` to `https://…`

TLS terminates at CloudFront and the ALB listener is plain HTTP, so apps that
build redirect URLs from the incoming request (Open WebUI, LiteLLM) emit
absolute `http://<domain>:8080|4000/…` `Location` headers — which the browser
can't reach on CloudFront, so the UI hangs. They ignore `X-Forwarded-Proto` for
these framework/app redirects, so no header or per-app base-URL setting fixes it.
A **viewer-response CloudFront Function** (`edge.tf`) rewrites any
`http://<host>:<port>/path` `Location` to `https://<host>/path`. Generic; no app
or ALB-listener changes. **Symptom if missing: LiteLLM UI / Open WebUI hang after
login with an `http://…:8080` URL in the address bar.**

### 3. OIDC apps need their public callback wired explicitly

Each app's OIDC redirect base must be the public URL, or Cognito rejects the
`redirect_uri`:
- **LiteLLM** uses `PROXY_BASE_URL` (set from the `litellm-public-url` secret).
- **Langfuse** uses `NEXTAUTH_URL`; its reconciler CronJob prefers the
  `langfuse-public-url` secret (edge) over the ALB hostname.
- **Open WebUI** derives its `redirect_uri` from the request too (→ the wrong
  `http://…:8080` value), so `OPENID_REDIRECT_URI` is set explicitly from a
  Terraform-filled secret. `ENABLE_LOGIN_FORM=false` makes it go straight to SSO.

**Symptom if missing: Cognito shows "An error was encountered with the requested
page" at login.**

## Other front doors

- **Tunnel (default):** `./platformctl tunnel` — SSM port-forward straight to the
  pods; works regardless of ALB scheme, no public exposure.
- **Your own domain:** `./platformctl edge domain` prints the guided GitOps steps
  (internet-facing ALB + your ACM cert + `sso_public_urls`). The same three
  concerns apply; wire your domain URLs into `sso_public_urls` so the OIDC
  redirects resolve.
