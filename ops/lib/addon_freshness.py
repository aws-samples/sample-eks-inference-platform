#!/usr/bin/env python3
"""Detect EKS *managed* addons running behind the latest available version.

This is the report-only companion to the Terraform `most_recent = true` intent
(terraform/30.eks/30.cluster): Terraform converges addons to latest on `apply`,
but drift accrues between applies — which is exactly how aws-ebs-csi-driver once
sat on a vulnerable pinned version. A weekly run of this detector surfaces that
drift as a GitHub issue so an operator applies it deliberately.

**It never mutates anything** — no `update-addon`, no `apply`. It only reads.

**Scope is inherently EKS MANAGED ADDONS ONLY.** It talks to the EKS Addons API
(ListAddons / DescribeAddon / DescribeAddonVersions), which knows nothing about
the Helm-/manifest-managed OSS components (LiteLLM, Open WebUI, Langfuse,
Karpenter, KEDA, the AWS Load Balancer Controller, …). Those are deliberately
operator-pinned and can never be in scope here — the tool cannot even see them.

Usage:
  addon_freshness.py --cluster NAME [--region us-east-1] [--format human|json|markdown]
                     [--fail-on-drift]

Exit codes: 0 = ok (or drift found, unless --fail-on-drift); 2 = drift found and
--fail-on-drift set; 1 = error (e.g. cluster not found / no credentials).
"""
from __future__ import annotations

import argparse
import json
import re
import sys


def _version_key(v: str | None) -> tuple:
    """Sortable key for an EKS addon version string, e.g. 'v1.65.0-eksbuild.1'.

    Returns (major, minor, patch, eksbuild). Missing pieces sort as 0, so this is
    safe on unexpected shapes rather than raising."""
    if not v:
        return (0, 0, 0, 0)
    s = v.lstrip("v")
    build_m = re.search(r"-eksbuild\.(\d+)", s)
    build = int(build_m.group(1)) if build_m else 0
    core = s.split("-", 1)[0]
    nums = [int(x) for x in re.findall(r"\d+", core)[:3]]
    nums += [0] * (3 - len(nums))
    return (nums[0], nums[1], nums[2], build)


def _newest(versions: list[str]) -> str | None:
    """The highest version by _version_key — i.e. what `most_recent = true` picks."""
    return max(versions, key=_version_key) if versions else None


def _addon_versions(eks, addon_name: str, k8s_version: str) -> list[str]:
    """All addon versions compatible with the cluster's Kubernetes version."""
    versions: list[str] = []
    paginator = eks.get_paginator("describe_addon_versions")
    for page in paginator.paginate(addonName=addon_name, kubernetesVersion=k8s_version):
        for addon in page.get("addons", []):
            for av in addon.get("addonVersions", []):
                v = av.get("addonVersion")
                if v:
                    versions.append(v)
    return versions


def detect(cluster: str, region: str | None = None) -> dict:
    """Compare every managed addon's live version to the newest compatible one.

    Returns a report dict: {cluster, region, k8sVersion, addons: [...], driftCount}.
    Each addon row is {name, current, latest, behind, status}."""
    import boto3  # imported lazily so --help / unit tests need no boto3

    eks = boto3.client("eks", region_name=region) if region else boto3.client("eks")
    cl = eks.describe_cluster(name=cluster)["cluster"]
    k8s_version = cl["version"]

    names: list[str] = []
    paginator = eks.get_paginator("list_addons")
    for page in paginator.paginate(clusterName=cluster):
        names.extend(page.get("addons", []))

    rows = []
    for name in sorted(names):
        addon = eks.describe_addon(clusterName=cluster, addonName=name)["addon"]
        current = addon.get("addonVersion")
        latest = _newest(_addon_versions(eks, name, k8s_version))
        behind = bool(latest) and _version_key(current) < _version_key(latest)
        rows.append({
            "name": name,
            "current": current,
            "latest": latest,
            "behind": behind,
            "status": addon.get("status"),
        })

    return {
        "cluster": cluster,
        "region": region or eks.meta.region_name,
        "k8sVersion": k8s_version,
        "addons": rows,
        "driftCount": sum(1 for r in rows if r["behind"]),
    }


def render_human(rep: dict) -> str:
    lines = [
        f"EKS managed-addon freshness — cluster '{rep['cluster']}' "
        f"({rep['region']}, k8s {rep['k8sVersion']})",
    ]
    for r in rep["addons"]:
        flag = "BEHIND" if r["behind"] else "ok"
        arrow = f"  ->  {r['latest']}" if r["behind"] else ""
        lines.append(f"  [{flag:>6}] {r['name']:<24} {r['current']}{arrow}")
    lines.append("")
    lines.append(f"{rep['driftCount']} addon(s) behind latest." if rep["driftCount"]
                 else "All managed addons are on the latest available version.")
    return "\n".join(lines)


def render_markdown(rep: dict) -> str:
    """GitHub-issue body: a table + the exact (operator-run) update commands.

    Report-only: these commands are for a human to run deliberately — the
    workflow never executes them."""
    head = (f"### EKS managed-addon freshness — `{rep['cluster']}`\n\n"
            f"- **Region:** `{rep['region']}`  •  **Kubernetes:** `{rep['k8sVersion']}`\n"
            f"- **Addons behind latest:** **{rep['driftCount']}** of {len(rep['addons'])}\n\n")
    if not rep["driftCount"]:
        return head + "✅ All managed addons are on the latest available version. Nothing to do.\n"

    table = ["| Addon | Current | Latest | Status |", "|---|---|---|---|"]
    cmds = []
    for r in rep["addons"]:
        mark = "⚠️ **behind**" if r["behind"] else "✅ current"
        table.append(f"| `{r['name']}` | `{r['current']}` | `{r['latest']}` | {mark} |")
        if r["behind"]:
            cmds.append(
                f"aws eks update-addon --cluster-name {rep['cluster']} "
                f"--region {rep['region']} \\\n"
                f"  --addon-name {r['name']} --addon-version {r['latest']} \\\n"
                f"  --resolve-conflicts PRESERVE")
    body = head + "\n".join(table) + "\n\n"
    body += ("<details><summary>Remediation (run deliberately — this report does "
             "<b>not</b> apply anything)</summary>\n\n")
    body += ("`--resolve-conflicts PRESERVE` keeps each addon's existing "
             "configuration values. Terraform's `most_recent = true` "
             "(terraform/30.eks/30.cluster) will also converge these on the next "
             "`terraform apply`.\n\n```bash\n" + "\n\n".join(cmds) + "\n```\n\n</details>\n")
    body += ("\n> Scope: EKS **managed addons only**. OSS components (LiteLLM, "
             "Open WebUI, Langfuse, Karpenter, KEDA, …) are intentionally pinned "
             "and are not part of this report.\n")
    return body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Detect EKS managed addons behind latest (report-only).")
    ap.add_argument("--cluster", required=True, help="EKS cluster name")
    ap.add_argument("--region", default=None, help="AWS region (defaults to the boto3 session region)")
    ap.add_argument("--format", choices=("human", "json", "markdown"), default="human")
    ap.add_argument("--fail-on-drift", action="store_true",
                    help="exit 2 if any addon is behind (for gating; the weekly workflow does NOT set this)")
    args = ap.parse_args(argv)

    try:
        rep = detect(args.cluster, args.region)
    except Exception as e:  # noqa: BLE001 — surface any AWS/credential error cleanly
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(rep, indent=2))
    elif args.format == "markdown":
        print(render_markdown(rep))
    else:
        print(render_human(rep))

    if args.fail_on_drift and rep["driftCount"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
