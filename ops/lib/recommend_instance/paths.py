"""Single source of truth for the GitOps workloads directory layout.

Shared by render.py (where `--deploy` WRITES a model manifest) and gitops.py
(where `--undeploy` FINDS and deletes it). Keeping the directory map in one
place prevents the deploy/undeploy drift that made `--undeploy` fail for
vLLM-tier models: deploy wrote them under workloads/models/inference/ while
undeploy only searched the top level of workloads/models/.
"""

from __future__ import annotations

import os
import re

# Repo-relative roots.
WORKLOADS_ROOT = "workloads"
MODELS_ROOT = "workloads/models"             # VLLMEndpoint tree: inference/ + team-*/
SCALE_MODELS_DIR = "workloads/scale-models"  # LLMDEndpoint / LLMDDisaggEndpoint (flat)

# Where `--deploy` writes each kind's manifest. VLLMEndpoints default to the
# platform-shared `inference` namespace directory; the llm-d tiers live flat in
# scale-models. Team-scoped vLLM models live under workloads/models/team-<name>/
# and are placed manually — `--undeploy` still finds them via the recursive
# search in find_model_files().
KIND_DIR = {
    "VLLMEndpoint": f"{MODELS_ROOT}/inference",
    "LLMDEndpoint": SCALE_MODELS_DIR,
    "LLMDDisaggEndpoint": SCALE_MODELS_DIR,
}

# A model name doubles as a Kubernetes object name, so it must be an RFC 1123
# label. Validating it before it touches a filesystem path ALSO blocks path
# traversal in `--undeploy NAME` (no '/', no '..', no absolute paths).
# NOTE: anchor with \A...\Z, not ^...$ — Python's $ also matches just BEFORE a
# trailing "\n", which would let a value like "name\n" slip through and inject a
# line into the emitted YAML manifest. \Z matches only the true end of string.
_MODEL_NAME = re.compile(r"\A[a-z0-9]([a-z0-9-]*[a-z0-9])?\Z")

# A Hugging Face model id: `org/name` (or a bare canonical `name`). The charset
# is letters/digits plus . _ - and a single slash — deliberately NO quotes,
# spaces, or newlines, so a validated id is safe to interpolate into a YAML
# manifest that gets committed and applied by ArgoCD (blocks YAML injection).
_HF_MODEL_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?\Z")


def is_valid_model_name(name: str) -> bool:
    """True if `name` is a safe RFC 1123 label (and therefore path-traversal-safe)."""
    return bool(name) and len(name) <= 63 and _MODEL_NAME.match(name) is not None


def is_valid_hf_model_id(model_id: str) -> bool:
    """True if `model_id` is a well-formed Hugging Face id safe to embed in YAML."""
    return bool(model_id) and len(model_id) <= 200 and _HF_MODEL_ID.match(model_id) is not None


def find_model_files(root: str, name: str) -> list[str]:
    """Repo-relative paths of every ``<name>.yaml`` under the models tree and the
    scale-models dir.

    Recursive under MODELS_ROOT so it finds both the platform-default
    ``inference/`` placement AND team ``team-<name>/`` placements; flat for
    scale-models. Caller MUST validate `name` with is_valid_model_name first.
    """
    matches: list[str] = []
    flat = os.path.join(SCALE_MODELS_DIR, f"{name}.yaml")
    if os.path.isfile(os.path.join(root, flat)):
        matches.append(flat)
    models_abs = os.path.join(root, MODELS_ROOT)
    if os.path.isdir(models_abs):
        for dirpath, _dirs, files in os.walk(models_abs):
            if f"{name}.yaml" in files:
                matches.append(os.path.relpath(os.path.join(dirpath, f"{name}.yaml"), root))
    return sorted(matches)


def list_deployed_models(root: str) -> list[tuple[str, str]]:
    """``(name, repo-relative-path)`` for every deployed model manifest across the
    models tree and scale-models dir, excluding ``*.example`` templates."""
    out: list[tuple[str, str]] = []
    for base in (MODELS_ROOT, SCALE_MODELS_DIR):
        base_abs = os.path.join(root, base)
        if not os.path.isdir(base_abs):
            continue
        for dirpath, _dirs, files in os.walk(base_abs):
            for f in files:
                if f.endswith(".yaml") and not f.endswith(".example"):
                    rel = os.path.relpath(os.path.join(dirpath, f), root)
                    out.append((f[:-5], rel))
    return sorted(out)
