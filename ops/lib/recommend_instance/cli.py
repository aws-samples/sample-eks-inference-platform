"""Command-line entrypoint: argument parsing and orchestration."""

from __future__ import annotations

import argparse
import os
import sys

from . import __doc__ as _PKG_DOC
from .catalog import KV_BYTES, TP_DEGREES, WEIGHT_BYTES
from .model import fetch_model
from .pricing import detect_region, resolve_prices
from .recommend import find_options
from .render import print_human, print_json
from .scaling import (
    DEFAULT_TARGET_TOK_S,
    DEFAULT_UTILIZATION_FACTOR,
    WORKLOAD_PRESETS,
    ScalingRecommendation,
    _max_concurrency_for_option,
    _slo_capacity,
    compute_scaling,
)
from .vram import estimate_vram

# main() reads module-level __doc__ to build the --help epilog; rebind it to the
# package docstring (which carries the Examples section) so --help is unchanged.
__doc__ = _PKG_DOC


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Recommend EC2 GPU instances for serving an LLM on EKS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:", 1)[1] if "Examples:" in (__doc__ or "") else "",
    )
    p.add_argument("model", nargs="?", default=None,
                   help="HuggingFace model ID (e.g. google/gemma-3-4b-it). "
                        "Not required with --undeploy.")
    p.add_argument("--quant", choices=sorted(WEIGHT_BYTES),
                   default="bf16", help="Weights quantisation (default: bf16)")
    p.add_argument("--kv-quant", choices=sorted(KV_BYTES),
                   default="fp16", help="KV cache quantisation (default: fp16)")
    p.add_argument("--seq", type=int, default=8192,
                   help="Max sequence length / maxModelLen (default: 8192)")
    p.add_argument("--workload", choices=sorted(WORKLOAD_PRESETS),
                   default=None,
                   help="Workload preset that fills sensible defaults for "
                        "--avg-context, --target-tok-s, and --users. "
                        "Override any individual field by setting it explicitly. "
                        "Choices: " + ", ".join(
                            f"{k}={v['description']}"
                            for k, v in WORKLOAD_PRESETS.items()
                        ))
    p.add_argument("--avg-context", type=int, default=None,
                   help="Typical input+output tokens per active sequence (default: --seq // 4). "
                        "Used for concurrency calculations to reflect PagedAttention reality "
                        "(KV is allocated per token in flight, not reserved up to max_model_len). "
                        "Set equal to --seq for worst-case concurrency sizing.")
    p.add_argument("--batch", type=int, default=1,
                   help="Batch size per step (default: 1)")
    p.add_argument("--users", type=int, default=1,
                   help="Total concurrent users to serve (default: 1). "
                        "If one instance can handle all users, recommends a single instance. "
                        "Otherwise, auto-scales to a fleet of replicas.")
    p.add_argument("--tp", type=int, choices=TP_DEGREES,
                   help="Pin tensor-parallel degree (1|2|4|8). Default: consider all.")
    p.add_argument("--region", default=None,
                   help="AWS region for pricing (default: $AWS_REGION, $AWS_DEFAULT_REGION, "
                        "boto3 session, or us-east-1)")
    p.add_argument("--max-price", type=float, default=20.0,
                   help="Per-hour price ceiling in USD. Options above are still shown "
                        "but flagged (default: 20.0)")
    p.add_argument("--refresh-prices", action="store_true",
                   help="Ignore cached prices and refetch from AWS Pricing API")
    p.add_argument("--in-cluster-only", action="store_true",
                   help="Only show instances allowed by this cluster's Karpenter NodePools")
    p.add_argument("--safety-margin", type=float, default=0.10,
                   help="Leave this fraction of GPU VRAM unused (default: 0.10)")
    p.add_argument("--limit", type=int, default=10,
                   help="Max options to display (default: 10)")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p.add_argument("--verbose", action="store_true",
                   help="Print pricing API fallbacks and other diagnostics to stderr")
    p.add_argument("--utilization", type=float, default=DEFAULT_UTILIZATION_FACTOR,
                   help=f"Target utilization factor for scaling mode — fraction of max "
                        f"concurrency to plan for (default: {DEFAULT_UTILIZATION_FACTOR}). "
                        f"Lower = more burst headroom.")
    p.add_argument("--target-tok-s", type=float, default=DEFAULT_TARGET_TOK_S,
                   help="Per-user decode throughput SLO in fleet mode (default: disabled). "
                        "When set, caps per-instance concurrency so each user meets this "
                        "throughput — forces escalation to faster GPUs if needed.")
    p.add_argument("--hf-token", default=os.environ.get("HF_TOKEN"),
                   help="HuggingFace token (also $HF_TOKEN). Required for gated models.")
    p.add_argument("--tier", choices=["auto", "vllm", "llm-d", "llm-d-disagg"], default="auto",
                   help="Serving CRD to emit: vllm=VLLMEndpoint, llm-d=LLMDEndpoint "
                        "(scale tier), llm-d-disagg=LLMDDisaggEndpoint (prefill/decode split). "
                        "Default auto: single->vllm, fleet->llm-d, "
                        "long-context fleet->llm-d-disagg.")
    p.add_argument("--deploy", action="store_true",
                   help="Write the recommended endpoint YAML under workloads/, then "
                        "git commit + push so ArgoCD deploys it (instead of only "
                        "printing the snippet).")
    p.add_argument("--undeploy", metavar="NAME", default=None,
                   help="Delete the model's <NAME>.yaml (found recursively under "
                        "workloads/models/ — inference/ or team-*/ — or in "
                        "workloads/scale-models/) and git commit + push so ArgoCD "
                        "removes the model (and LiteLLM deregisters it). Operates on "
                        "the file by name — no model lookup; the positional model arg "
                        "is not required with --undeploy.")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip the confirmation prompt for --deploy/--undeploy.")
    args = p.parse_args(argv)

    # --undeploy is a pure file/git operation: short-circuit before any HF fetch
    # or pricing call (no model metadata needed to delete a manifest).
    if args.undeploy is not None:
        from .gitops import undeploy_model
        return undeploy_model(args.undeploy, args)

    # Every other path needs a model to size/recommend.
    if not args.model:
        p.error("the 'model' argument is required (unless using --undeploy)")

    if not 0.0 <= args.safety_margin < 0.5:
        sys.exit("error: --safety-margin must be in [0.0, 0.5)")
    if args.max_price <= 0:
        sys.exit("error: --max-price must be > 0")
    if not 0.1 <= args.utilization <= 1.0:
        sys.exit("error: --utilization must be in [0.1, 1.0]")
    if args.target_tok_s < 0:
        sys.exit("error: --target-tok-s must be >= 0")
    if args.avg_context is not None and args.avg_context < 1:
        sys.exit("error: --avg-context must be >= 1")

    # Internal fleet-mode flag — set programmatically, not by CLI.
    args.target_users = None

    if args.users < 1:
        sys.exit("error: --users must be >= 1")

    # Apply workload preset (only fills fields the user didn't explicitly set).
    args._preset_applied: list[str] = []
    if args.workload:
        preset = WORKLOAD_PRESETS[args.workload]
        if args.avg_context is None:
            args.avg_context = preset["avg_context"]
            args._preset_applied.append(f"--avg-context {preset['avg_context']}")
        if args.target_tok_s == DEFAULT_TARGET_TOK_S:
            args.target_tok_s = float(preset["target_tok_s"])
            args._preset_applied.append(f"--target-tok-s {preset['target_tok_s']}")
        if args.users == 1:
            args.users = preset["users"]
            args._preset_applied.append(f"--users {preset['users']}")

    args.region = detect_region(args.region)
    prices, price_src = resolve_prices(args.region, args.refresh_prices, args.verbose)

    # Always size VRAM for 1 user (minimal fit check). The user count is
    # handled by fleet scaling, not by inflating the per-instance VRAM estimate.
    model = fetch_model(args.model, args.hf_token)
    vram  = estimate_vram(model, args.quant, args.kv_quant, args.seq,
                          args.batch, 1,
                          avg_context_len=args.avg_context)
    opts  = find_options(
        total_need_gb=vram.total_gb,
        num_heads=model.num_heads,
        tp_pin=args.tp,
        require_in_cluster=args.in_cluster_only,
        safety_margin=args.safety_margin,
        prices=prices,
        max_price=args.max_price,
        model=model,
        weight_q=args.quant,
        kv_q=args.kv_quant,
        users=1,
    )
    best  = opts[0] if opts else None

    # Auto-fleet decision: can the best instance handle all requested users
    # on its own? If yes → single-instance. If no → fleet mode.
    # With --target-tok-s set, also check the SLO capacity.
    scaling: list[ScalingRecommendation] | None = None

    if best is not None and opts and args.users > 1:
        max_conc = _max_concurrency_for_option(best, vram, args.safety_margin)
        effective_cap = max(1, int(max_conc * args.utilization))

        # SLO check: does the best instance meet the tok/s target at this load?
        if args.target_tok_s > 0:
            slo_cap, _, slo_unmet = _slo_capacity(
                best.single_stream_tok_s, args.target_tok_s, effective_cap,
            )
            effective_cap = min(effective_cap, slo_cap)

        if args.users > effective_cap:
            # Best single instance can't handle all users → fleet mode
            args.target_users = args.users
            scaling = compute_scaling(
                opts=opts,
                vram=vram,
                target_users=args.target_users,
                utilization_factor=args.utilization,
                safety_margin=args.safety_margin,
                target_tok_s=args.target_tok_s,
            )

    if args.json:
        print_json(model, vram, opts, best, args, price_src, scaling)
    else:
        print_human(model, vram, opts, best, args, price_src, scaling, prices)

    # --deploy: write the recommended manifest + commit + push. Requires a real
    # recommendation — refuse if nothing fits within the constraints.
    if args.deploy:
        if best is None:
            sys.stderr.write("\ncannot --deploy: no instance fits the model within "
                             "the given constraints (raise --max-price or adjust flags).\n")
            return 2
        from .gitops import deploy_model
        from .render import build_endpoint_yaml, pick_tier, resolve_kind
        kind = resolve_kind(pick_tier(args, best, scaling), args, scaling)
        name, yaml_path, yaml_body, commit_msg = build_endpoint_yaml(
            kind, model, vram, best, args, scaling)
        return deploy_model(name, yaml_path, yaml_body, commit_msg, args)

    return 0 if best else 2
