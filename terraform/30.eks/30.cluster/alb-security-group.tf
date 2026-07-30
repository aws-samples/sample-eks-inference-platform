################################################################################
# ALB frontend security group (shared by the four platform UI ingresses)
#
# WHY WE OWN THIS SG instead of the controller's `inbound-cidrs`:
# The CloudFront edge reaches the INTERNAL ALB through a VPC origin whose traffic
# is authorized by SECURITY-GROUP REFERENCE, not by the VPC CIDR. The AWS Load
# Balancer Controller fully manages the SG it auto-creates from `inbound-cidrs`
# and strips any foreign rule — so a CIDR rule can't authorize the VPC origin and
# we can't durably add an SG-reference rule to the managed SG. Instead the
# ingresses reference THIS SG via `alb.ingress.kubernetes.io/security-groups`, and
# the CloudFront VPC-origin allow-rule lives here (see edge.tf). The controller
# still auto-manages the backend SG (v2.7), so ALB->target traffic is unaffected.
#
# Fixed name so the static ingress manifests can reference it by name in any fork
# (SG names are unique per VPC; one platform ALB per VPC).
################################################################################

locals {
  # UI -> ALB listener port. Must stay in sync with local.edge_ports (edge.tf)
  # and the four ingresses (platform/config/ingress.yaml + cluster-dashboard).
  alb_ui_ports = [8080, 4000, 3000, 9090]
}

resource "aws_security_group" "alb_frontend" {
  name        = "ai-platform-alb-inbound"
  description = "Inbound for the ai-platform UIs shared ALB (VPC CIDR + CloudFront VPC origin)"
  vpc_id      = data.terraform_remote_state.vpc.outputs.vpc_id
  tags        = merge(local.tags, { Name = "ai-platform-alb-inbound" })
}

# The platform VPC's CIDR (read from the VPC itself so it always matches, with no
# dependency on a variable being declared in this stage).
data "aws_vpc" "this" {
  id = data.terraform_remote_state.vpc.outputs.vpc_id
}

# In-VPC access on each UI port (SSM tunnel node, a bastion, in-VPC clients) —
# this replaces the controller-managed `inbound-cidrs`.
resource "aws_vpc_security_group_ingress_rule" "alb_frontend_vpc" {
  for_each = toset([for p in local.alb_ui_ports : tostring(p)])

  security_group_id = aws_security_group.alb_frontend.id
  cidr_ipv4         = data.aws_vpc.this.cidr_block
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
  ip_protocol       = "tcp"
  description       = "in-VPC access to the platform UIs"
}

resource "aws_vpc_security_group_egress_rule" "alb_frontend_all" {
  security_group_id = aws_security_group.alb_frontend.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "ALB to targets / all egress"
}
