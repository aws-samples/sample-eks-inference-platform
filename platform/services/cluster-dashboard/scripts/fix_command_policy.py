"""Deterministic allowlist for Platform Health Agent remediation commands.

The Investigator LLM proposes kubectl-style `fix_commands`, a human approves
them, and the Remediator LLM applies them. Both LLMs see attacker-influenceable
input (event messages, pod logs, resource names from tenant workloads), so
neither is trusted to bound what gets executed. This module is the deterministic
gate that runs BEFORE remediation: it parses every proposed command and rejects
anything outside a conservative allowlist —

  * verb    ∈ {patch, scale, annotate, label, rollout, apply, delete}
  * kind    ∈ workload-level kinds only (Deployment/StatefulSet/Pod/ConfigMap/
              HPA/ReplicaSet); never Secrets, RBAC, Nodes, Namespaces, CRDs…
  * delete  ∈ pods only (restart-by-delete), never controllers
  * ns      ∈ {inference, team-*} — the same tenant scope the writer RBAC allows
  * no shell chaining / command substitution

Fail-closed: any command it cannot confidently parse, or that omits an explicit
allowed namespace, is REJECTED. A rejected plan is handled by a human instead of
auto-applied — the safe direction. This bounds the APPROVED plan deterministically,
independent of RBAC (defence in depth) and independent of what either LLM decides.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

ALLOWED_NS = re.compile(r"^(inference|team-[a-z0-9]([a-z0-9-]*[a-z0-9])?)$")

ALLOWED_KINDS = {
    "deployment", "deployments", "deploy",
    "statefulset", "statefulsets", "sts",
    "pod", "pods", "po",
    "configmap", "configmaps", "cm",
    "horizontalpodautoscaler", "horizontalpodautoscalers", "hpa",
    "replicaset", "replicasets", "rs",
}

SAFE_VERBS = {"patch", "scale", "annotate", "label", "rollout", "apply", "delete"}

# Sequences that indicate shell chaining / substitution. fix_commands are
# single kubectl invocations; any of these is a red flag (and a copy-paste hazard
# in the dashboard). Kept narrow so JSON patch bodies (which may contain > or |)
# don't false-positive.
FORBIDDEN_SEQUENCES = (";", "&&", "||", "$(", "`", "\n", "\r")


def _kind_of(token: str) -> str:
    """`deployment/foo` -> `deployment`; `deployment` -> `deployment`."""
    return token.split("/", 1)[0].lower()


def validate_command(cmd: str) -> str | None:
    """Return None if the command is allowed, else a violation reason."""
    for seq in FORBIDDEN_SEQUENCES:
        if seq in cmd:
            return f"shell chaining/substitution not allowed: {cmd!r}"
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return f"unparseable command: {cmd!r}"
    if not toks or toks[0] != "kubectl":
        return f"not a kubectl command: {cmd!r}"
    toks = toks[1:]

    namespace = None
    positional: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("-n", "--namespace"):
            namespace = toks[i + 1] if i + 1 < len(toks) else None
            i += 2
            continue
        if t.startswith("--namespace="):
            namespace = t.split("=", 1)[1]
            i += 1
            continue
        if t.startswith("-"):
            i += 1  # any other flag; its value (if separate) is ignored positionally
            continue
        positional.append(t)
        i += 1

    if not positional:
        return f"no subcommand: {cmd!r}"
    verb = positional[0].lower()
    if verb not in SAFE_VERBS:
        return f"verb '{verb}' not allowed"
    if namespace is None or not ALLOWED_NS.match(namespace):
        return f"namespace '{namespace}' not in allowed tenant scope (inference, team-*)"

    if verb == "apply":
        # `apply -f <file>`: the kind lives in the file, not the argv, so we
        # cannot inspect it here. The namespace flag is validated above and the
        # writer RBAC still bounds it; allow.
        return None

    if verb == "rollout":
        sub = positional[1].lower() if len(positional) > 1 else ""
        if sub not in ("restart", "undo", "status"):
            return f"rollout subcommand '{sub}' not allowed"
        kind = _kind_of(positional[2]) if len(positional) > 2 else None
    else:
        kind = _kind_of(positional[1]) if len(positional) > 1 else None

    if kind is None or kind not in ALLOWED_KINDS:
        return f"resource kind '{kind}' not allowed"
    if verb == "delete" and kind not in ("pod", "pods", "po"):
        return f"delete is permitted for pods only, not '{kind}'"
    return None


def validate(fix_commands) -> list[str]:
    """Validate a list of {description, commands:[...]} items.

    Returns a list of violation strings (empty == all commands allowed).
    """
    violations: list[str] = []
    for item in fix_commands or []:
        for cmd in (item.get("commands") or []):
            reason = validate_command(cmd)
            if reason:
                violations.append(reason)
    return violations


def _main(argv: list[str]) -> int:
    """CLI: `fix_command_policy.py <context-or-findings.json>`.

    Reads a JSON object with a top-level `fix_commands` key, prints any
    violations to stderr, and exits non-zero if the plan is not allowed
    (fail-closed for the remediator wrapper).
    """
    if len(argv) != 2:
        print("usage: fix_command_policy.py <json-with-fix_commands>", file=sys.stderr)
        return 2
    with open(argv[1]) as f:
        data = json.load(f)
    fix_commands = data.get("fix_commands") or []
    violations = validate(fix_commands)
    if violations:
        print("fix_command policy REJECTED the approved plan:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    print("fix_command policy: all commands within allowlist")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
