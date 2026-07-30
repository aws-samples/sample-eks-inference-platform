"""GitOps deploy / undeploy: write (or delete) a model's serving endpoint YAML
and commit + push it, so ArgoCD applies (or prunes) the change.

Both paths are surgical: they stage ONLY the one model file (never a blanket
`git add`), so an unrelated dirty working tree is left untouched. A push failure
leaves a local commit behind — reported, not hidden — so the user can retry.

After a successful push we trigger an *explicit* ArgoCD sync of the `models`
app pinned to the pushed commit (best-effort, via kubectl). This does two things
the plain push could not: it applies the change in seconds instead of waiting
for ArgoCD's ~3-minute git poll, and — crucially for --undeploy — an explicit
sync bypasses ArgoCD's automated-sync guard that REFUSES to prune an app down to
zero resources. That guard is why deleting the *last* model's YAML used to leave
its serving endpoint stranded: the dir rendered no manifests, so auto-sync
skipped the prune. A manual sync has no such restriction.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from .paths import (
    MODELS_ROOT,
    SCALE_MODELS_DIR,
    find_model_files,
    is_valid_model_name,
    list_deployed_models,
)
from .ux import _palette, _should_use_colour

# ArgoCD Application naming for deploy/undeploy sync targets. The
# workloads-models ApplicationSet uses a git *directory* generator: it creates
# ONE Application per workloads/models/<namespace>/ subdir, named
# "models-<namespace>" (e.g. models-inference, models-team-foo). There is no
# single "models" app — the per-namespace name is derived in _app_for_path;
# this constant is just the shared prefix.
ARGOCD_NAMESPACE = "argocd"
ARGOCD_MODELS_APP = "models"
# The llm-d scale tier lives in a separate dir owned by its own ArgoCD app.
ARGOCD_SCALE_MODELS_APP = "scale-models"


def _app_for_path(yaml_path: str) -> str:
    # Scale tier (workloads/scale-models/) is a single list-generator app.
    if yaml_path.startswith(SCALE_MODELS_DIR):
        return ARGOCD_SCALE_MODELS_APP
    # vLLM tier: workloads/models/<namespace>/<name>.yaml -> "models-<namespace>"
    # (one app per subdir, from the workloads-models directory generator).
    ns = os.path.relpath(yaml_path, MODELS_ROOT).split(os.sep)[0]
    return f"{ARGOCD_MODELS_APP}-{ns}"


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def _repo_root() -> str:
    """Resolve the git repo root from this file's location (works regardless of
    the caller's CWD)."""
    here = os.path.dirname(os.path.abspath(__file__))
    r = _run(["git", "rev-parse", "--show-toplevel"], cwd=here)
    if r.returncode != 0:
        sys.exit(f"error: not inside a git repository ({r.stderr.strip()})")
    return r.stdout.strip()


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        # Non-interactive without --yes → refuse rather than act silently.
        sys.stderr.write("refusing to proceed without a TTY; pass --yes to confirm.\n")
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _git_commit_push(root: str, rel_path: str, commit_msg: str, C: type) -> int:
    """Stage exactly `rel_path` (a create, modify, OR delete), commit, and push.
    Returns a process exit code. `-A` is required so a deletion stages cleanly —
    a plain `git add -- <path>` errors with 'pathspec did not match' once the
    file is gone, which is what silently broke --undeploy."""
    add = _run(["git", "add", "-A", "--", rel_path], cwd=root)
    if add.returncode != 0:
        print(f"{C.RED}git add failed:{C.RESET} {add.stderr.strip()}", file=sys.stderr)
        return 1

    # Nothing staged (e.g. file content identical / already absent) → no-op.
    staged = _run(["git", "diff", "--cached", "--quiet", "--", rel_path], cwd=root)
    if staged.returncode == 0:
        print(f"{C.YELLOW}No change to commit{C.RESET} — {rel_path} already in the "
              f"desired state. Nothing pushed.")
        return 0

    commit = _run(["git", "commit", "-m", commit_msg, "--", rel_path], cwd=root)
    if commit.returncode != 0:
        print(f"{C.RED}git commit failed:{C.RESET} {commit.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"{C.GREEN}✓ committed{C.RESET} {commit_msg}")

    push = _run(["git", "push"], cwd=root)
    if push.returncode != 0:
        print(f"{C.RED}git push failed:{C.RESET} {push.stderr.strip()}", file=sys.stderr)
        print(f"{C.DIM}The commit is saved locally — fix the remote/auth and "
              f"`git push` to apply it.{C.RESET}", file=sys.stderr)
        return 1
    print(f"{C.GREEN}✓ pushed{C.RESET}")
    return 0


def _pushed_sha(root: str) -> str | None:
    """Resolve the commit just pushed, so the ArgoCD sync can be pinned to it
    (avoids a race where ArgoCD syncs a stale cached HEAD)."""
    r = _run(["git", "rev-parse", "HEAD"], cwd=root)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _argocd_sync(root: str, C: type, app: str = ARGOCD_MODELS_APP) -> None:
    """Best-effort: trigger an explicit ArgoCD sync of the `models` app, pinned to
    the just-pushed commit, via `kubectl patch ... operation`.

    Why this exists (two reasons, both observed on --undeploy):
      1. Speed — skips ArgoCD's ~3-minute git poll; the change lands in seconds.
      2. Correctness — ArgoCD's *automated* sync refuses to prune an app to zero
         resources. Deleting the last model's YAML renders an empty dir, so
         auto-sync silently skips the prune and the serving endpoint is
         stranded. A *manual* sync operation has no empty-prune guard.

    This is advisory, never fatal: the push already succeeded (git is the source
    of truth). If kubectl is missing or the cluster is unreachable, we say so and
    fall back to ArgoCD's own polling — the command still returns success."""
    if shutil.which("kubectl") is None:
        print(f"{C.DIM}kubectl not found — ArgoCD will reconcile on its next "
              f"git poll (~3 min).{C.RESET}")
        return

    sha = _pushed_sha(root)
    # prune=true is mandatory for --undeploy (the resource must be removed).
    # Pin to the pushed SHA when known so ArgoCD doesn't sync a stale cached HEAD;
    # otherwise let it resolve the target branch's HEAD itself (still a manual
    # sync → still bypasses the empty-prune guard).
    sync_spec = {"prune": True}
    if sha:
        sync_spec["revision"] = sha
    operation = {"operation": {
        "initiatedBy": {"username": "recommend-instance"},
        "sync": sync_spec,
    }}

    patch = _run(
        ["kubectl", "patch", "application", app,
         "-n", ARGOCD_NAMESPACE, "--type", "merge", "-p", json.dumps(operation)],
        cwd=root,
    )
    if patch.returncode != 0:
        err = patch.stderr.strip()
        if "NotFound" in err or "not found" in err:
            # First model in this namespace: the "models-<ns>" app is created
            # per-namespace by the workloads-models ApplicationSet, which
            # reconciles the new workloads/models/<ns>/ directory into an
            # Application within ~a minute and auto-syncs it. Nothing to do.
            print(f"{C.DIM}ArgoCD app '{app}' doesn't exist yet — it's created "
                  f"per-namespace by the workloads-models ApplicationSet on the "
                  f"first model in that namespace, then auto-synced (~1 min). "
                  f"Nothing to do — the push already applied.{C.RESET}")
        else:
            print(f"{C.YELLOW}Could not trigger an ArgoCD sync{C.RESET} "
                  f"({err or 'kubectl failed'}).")
            print(f"{C.DIM}Not fatal — ArgoCD reconciles on its next git poll "
                  f"(~3 min), or run: "
                  f"argocd app sync {app} --prune{C.RESET}")
        return

    pin = f" @ {sha[:7]}" if sha else ""
    print(f"{C.GREEN}✓ ArgoCD sync triggered{C.RESET} ({app}{pin}, "
          f"prune) — applies in seconds.")


def deploy_model(name: str, yaml_path: str, yaml_body: str, commit_msg: str,
                 args) -> int:
    """Write the model YAML to the repo and commit + push it."""
    C = _palette(_should_use_colour(args))
    root = _repo_root()
    abs_path = os.path.join(root, yaml_path)

    action = "Overwrite" if os.path.exists(abs_path) else "Create"
    print(f"\n{C.BOLD}{action} {yaml_path}{C.RESET} → git push → ArgoCD deploys "
          f"{C.BOLD}{name}{C.RESET}.")

    # Show exactly what will be written (or, on overwrite, what changes) so the
    # user confirms with full sight of the manifest — not a blind [y/N].
    if os.path.exists(abs_path):
        import difflib
        with open(abs_path) as f:
            old = f.read()
        diff = list(difflib.unified_diff(
            old.splitlines(), yaml_body.splitlines(),
            fromfile="current", tofile="new", lineterm="",
        ))
        if diff:
            print(f"\n  {C.DIM}Changes vs the current manifest:{C.RESET}")
            for ln in diff[2:]:   # skip the ---/+++ file headers
                if ln.startswith("+"):
                    print(f"  {C.GREEN}{ln}{C.RESET}")
                elif ln.startswith("-"):
                    print(f"  {C.RED}{ln}{C.RESET}")
                else:
                    print(f"  {C.DIM}{ln}{C.RESET}")
        else:
            print(f"\n  {C.DIM}(identical to the current file — nothing would change){C.RESET}")
    else:
        print(f"\n  {C.DIM}Manifest to write:{C.RESET}")
        for ln in yaml_body.rstrip().splitlines():
            print(f"  {C.DIM}│{C.RESET} {ln}")

    if not _confirm("\nProceed?", args.yes):
        print("Aborted — nothing written.")
        return 1

    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w") as f:
        f.write(yaml_body)
    print(f"{C.GREEN}✓ wrote{C.RESET} {yaml_path}")

    rc = _git_commit_push(root, yaml_path, commit_msg, C)
    if rc == 0:
        _argocd_sync(root, C, _app_for_path(yaml_path))
        print(f"\n{C.DIM}Watch it come up:{C.RESET} "
              f"kubectl get vllmendpoints,llmdendpoints,llmddisaggendpoints -n inference -w")
        print(f"{C.DIM}Remove it later:{C.RESET} "
              f"./platformctl new-model --undeploy {name}")
    return rc


def undeploy_model(name: str, args) -> int:
    """Delete a model's YAML from the repo and commit + push so ArgoCD prunes the
    serving endpoint (which also deregisters it from LiteLLM via the finalizer).
    Needs no model lookup — operates purely on the file by name."""
    C = _palette(_should_use_colour(args))

    # Validate before touching the filesystem: `name` is an RFC 1123 label, which
    # also blocks path traversal (a "name" like ../../terraform/main can't escape
    # the workloads tree and get committed/pushed as a deletion).
    if not is_valid_model_name(name):
        print(f"{C.RED}Invalid model name:{C.RESET} {name!r} — must be a lowercase "
              f"DNS-1123 label (letters, digits, '-'), e.g. 'qwen3-32b'.", file=sys.stderr)
        return 2

    root = _repo_root()
    # Recursive search: matches the platform-default inference/ placement AND team
    # team-<name>/ placements (the deploy directory is shared via .paths.KIND_DIR).
    matches = find_model_files(root, name)
    if not matches:
        print(f"{C.RED}No such model file:{C.RESET} {name}.yaml "
              f"(searched {MODELS_ROOT}/ recursively and {SCALE_MODELS_DIR}/)")
        deployed = list_deployed_models(root)
        if deployed:
            print(f"{C.DIM}Deployed models:{C.RESET}")
            for m, rel in deployed:
                print(f"  {m}  {C.DIM}({rel}){C.RESET}")
        return 2
    if len(matches) > 1:
        print(f"{C.RED}Ambiguous:{C.RESET} '{name}' matches multiple manifests — "
              f"remove the specific file manually:", file=sys.stderr)
        for rel in matches:
            print(f"  {rel}", file=sys.stderr)
        return 2
    rel_path = matches[0]
    abs_path = os.path.join(root, rel_path)

    print(f"\n{C.BOLD}Delete {rel_path}{C.RESET} and push so ArgoCD removes "
          f"{C.BOLD}{name}{C.RESET} (the serving endpoint is pruned and LiteLLM "
          f"deregisters it).")
    if not _confirm("Proceed?", args.yes):
        print("Aborted — file left in place.")
        return 1

    # Remove the file on disk only — staging is handled uniformly by
    # _git_commit_push (`git add -A`). Do NOT `git rm` here: that pre-stages the
    # deletion, and the subsequent add then had nothing to match → the commit was
    # skipped and the file was left deleted-but-not-committed (the bug this fixes).
    try:
        os.remove(abs_path)
    except OSError as e:
        print(f"{C.RED}could not remove {rel_path}:{C.RESET} {e}", file=sys.stderr)
        return 1

    rc = _git_commit_push(root, rel_path, f"chore: undeploy {name}", C)
    if rc == 0:
        # The push alone is NOT enough on undeploy: if this was the last model,
        # workloads/models/ renders empty and ArgoCD's automated sync won't prune
        # to zero. The explicit sync below is what actually removes the endpoint.
        _argocd_sync(root, C, _app_for_path(rel_path))
        print(f"\n{C.DIM}Confirm removal:{C.RESET} "
              f"kubectl get vllmendpoints,llmdendpoints,llmddisaggendpoints -n inference")
    return rc
