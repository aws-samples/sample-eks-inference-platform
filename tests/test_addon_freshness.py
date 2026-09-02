"""Guard: every EKS *managed* addon must be version-controlled.

An EKS managed addon (aws-ebs-csi-driver) once sat on a vulnerable version because
its Terraform block declared NEITHER `most_recent = true` NOR an explicit
`addon_version` — so it silently used the module default and drifted. This test
fails the build if any managed addon is declared without a version directive
(either `most_recent = true`, which tracks latest, or an explicit `addon_version`
pin — both are deliberate; declaring neither is the bug).

Scope is deliberately **EKS MANAGED ADDONS ONLY** — the things the EKS Addons API
(describe-addon / update-addon) acts on. OSS components (LiteLLM, Open WebUI,
Langfuse, Karpenter, KEDA, the AWS Load Balancer Controller, …) are intentionally
PINNED and are NOT checked here: they live in Helm charts / manifests, a different
control plane entirely, and their versions must stay operator-controlled.
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLUSTER = os.path.join(REPO_ROOT, "terraform", "30.eks", "30.cluster")

# The EKS managed addons provisioned via the EKS module's `addons` map in
# 30.cluster/main.tf. Keep in sync with that map. (metrics-server is provisioned
# separately via an aws_eks_addon resource — checked in its own test below.)
MODULE_ADDONS = (
    "vpc-cni",
    "kube-proxy",
    "coredns",
    "eks-pod-identity-agent",
    "aws-ebs-csi-driver",
)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _addon_block(hcl, name):
    """Return the text of the `<name> = { … }` addon block, brace-balanced."""
    m = re.search(rf"(?m)^\s*{re.escape(name)}\s*=\s*\{{", hcl)
    assert m, f"addon block for '{name}' not found in main.tf"
    start = m.end() - 1  # position of the opening brace
    depth = 0
    for j in range(start, len(hcl)):
        if hcl[j] == "{":
            depth += 1
        elif hcl[j] == "}":
            depth -= 1
            if depth == 0:
                return hcl[start : j + 1]
    raise AssertionError(f"unbalanced braces for addon '{name}'")


class TestManagedAddonsTrackLatest:
    def test_module_addons_are_version_controlled(self):
        hcl = _read(os.path.join(CLUSTER, "main.tf"))
        for name in MODULE_ADDONS:
            block = _addon_block(hcl, name)
            most_recent = re.search(r"(?m)^\s*most_recent\s*=\s*true\b", block)
            pinned = re.search(r'(?m)^\s*addon_version\s*=\s*"', block)
            assert most_recent or pinned, (
                f"EKS managed addon '{name}' must be version-controlled — either "
                f"`most_recent = true` (tracks latest) or an explicit `addon_version` "
                f"pin. Declaring NEITHER is the aws-ebs-csi-driver drift that let it "
                f"silently sit on a vulnerable version."
            )

    def test_metrics_server_uses_most_recent_datasource(self):
        hcl = _read(os.path.join(CLUSTER, "metrics-server.tf"))
        assert 'data "aws_eks_addon_version"' in hcl, (
            "metrics-server should resolve its version from an aws_eks_addon_version "
            "data source."
        )
        assert re.search(r"(?m)^\s*most_recent\s*=\s*true\b", hcl), (
            "metrics-server's aws_eks_addon_version data source must set "
            "`most_recent = true`."
        )
