"""Presentation: human-readable report and machine-readable JSON output."""

from __future__ import annotations

import argparse
import json
import math
import sys

from .catalog import TIME_SLICE_REPLICAS
from .model import ModelSpec
from .paths import KIND_DIR, is_valid_hf_model_id, is_valid_model_name
from .recommend import Option, find_options
from .scaling import ScalingRecommendation, compute_scaling
from .throughput import _parallelism_efficiency, per_user_tok_s
from .ux import _bar, _fmt_monthly, _palette, _ruler, _should_use_colour
from .vram import VramEstimate, estimate_vram


# --------------------------------------------------------------------------- #
# Presentation                                                                #
# --------------------------------------------------------------------------- #

def _fmt_params(p: int) -> str:
    if p >= 1e12: return f"{p/1e12:.2f}T"
    if p >= 1e9:  return f"{p/1e9:.2f}B"
    if p >= 1e6:  return f"{p/1e6:.1f}M"
    return str(p)


def _mode_line(args: argparse.Namespace) -> str:
    """One-line summary of what mode the recommender is running in."""
    if args.target_users is not None:
        slo_part = (
            f"@ {args.target_tok_s:.0f} tok/s/user SLO"
            if args.target_tok_s > 0 else "no SLO"
        )
        return f"fleet sizing for {args.target_users} users {slo_part}"
    if args.tp is not None:
        return f"cheapest fit for {args.users} users, TP={args.tp} pinned"
    if args.users > 1:
        slo = f", {args.target_tok_s:.0f} tok/s SLO" if args.target_tok_s > 0 else ""
        return f"single instance for {args.users} users{slo}"
    return "cheapest fit (1 user)"


def _next_steps_lines(
    args:    argparse.Namespace,
    best:    "Option | None",
    scaling: "list[ScalingRecommendation] | None",
) -> list[str]:
    """Suggest next-step CLI flags the user might want to try.

    Returns a list of suggestion strings (one per useful next step).
    Suggestions are tailored to what the user *didn't* set, not generic.
    """
    suggestions: list[str] = []

    # Default mode: tell user how to add users/SLO
    if args.users <= 1:
        suggestions.append(
            "Production sizing:    --users N --target-tok-s X"
        )

    # No workload set → suggest one
    if not args.workload:
        suggestions.append(
            "Workload presets:  --workload chat | rag | code | summarization | batch"
        )

    # If shared-eligible single-GPU model → mention shared mode
    if best is not None and best.shared_eligible and best.total_gpus == 1 \
            and not (scaling and len(scaling) > 0):
        suggestions.append(
            f"Reduce $/model by {TIME_SLICE_REPLICAS}×:  add 'shared: true' "
            f"(time-slice up to {TIME_SLICE_REPLICAS} models per GPU)"
        )

    suggestions.append("Full options:  --help | --verbose")

    return suggestions[:4]


def _explain_recommendation(
    best: "Option",
    all_opts: list["Option"],
    args: argparse.Namespace,
    model: ModelSpec,
    scaling: "list[ScalingRecommendation] | None" = None,
) -> str:
    """Generate a single-sentence rationale for why `best` was recommended.

    The explanation walks down a hierarchy of "why":
      1. The dominant constraint (cost, throughput, or fit)
      2. Why this strategy (TP/PP/single-GPU)
      3. Any caveats (quant warning, near VRAM limit, shared mode)

    The runner-up — if there is one on a different instance type — is used
    as the comparator: "picked X over Y because ...". This makes the
    rationale concrete rather than abstract.

    In fleet mode (when `scaling` is provided), the rationale is fleet-aware:
    explains the replica count, per-instance load, and cost-per-fleet vs
    the cheapest alternative.
    """
    if best is None:
        return ""

    # In fleet mode, the recommendation is about the FLEET's chosen
    # (instance, tp, pp) — which may differ from the single-instance `best`
    # (the cheapest fitter at concurrency=1). Switch the explainer's anchor
    # to the fleet pick so the rationale matches the headline banner.
    fleet_best = scaling[0] if scaling else None
    is_fleet = args.target_users is not None and fleet_best is not None
    anchor = fleet_best.option if is_fleet else best

    # Find a meaningful runner-up on a different instance type.
    runner_up = None
    for o in all_opts:
        if o is anchor:
            continue
        if o.instance.name != anchor.instance.name:
            runner_up = o
            break

    parts: list[str] = []

    # --- Strategy explanation (TP vs PP vs single-GPU) -------------------- #
    if anchor.tp_degree > 1 and anchor.pp_degree > 1:
        strategy_phrase = (
            f"TP={anchor.tp_degree} × PP={anchor.pp_degree} on {anchor.total_gpus} GPUs "
            f"({'NVLink' if anchor.instance.has_nvlink else 'PCIe'})"
        )
    elif anchor.tp_degree > 1:
        nvlink = "NVLink" if anchor.instance.has_nvlink else "PCIe"
        strategy_phrase = (
            f"TP={anchor.tp_degree} on {nvlink} (aggregate "
            f"{anchor.instance.hbm_bandwidth_tb_s * anchor.tp_degree:.2f} TB/s HBM)"
        )
    elif anchor.pp_degree > 1:
        strategy_phrase = (
            f"PP={anchor.pp_degree} (sequential stages, no bandwidth aggregation)"
        )
    else:
        strategy_phrase = (
            f"single-GPU on {anchor.instance.gpu} "
            f"({anchor.instance.hbm_bandwidth_tb_s:.2f} TB/s HBM)"
        )

    if is_fleet:
        fb = fleet_best
        parts.append(
            f"{fb.replicas}× {anchor.instance.name} runs the model with {strategy_phrase}."
        )
        r_word = "replica" if fb.replicas == 1 else "replicas"
        parts.append(
            f"Each replica serves {fb.effective_capacity} concurrent users at "
            f"~{fb.estimated_tok_s_per_user:.0f} tok/s/user; "
            f"fleet of {fb.replicas} {r_word} covers {args.target_users} users at "
            f"${fb.fleet_cost_usd_h:.2f}/hr (~{_fmt_monthly(fb.fleet_cost_usd_h)}/month)."
        )
        # Fleet-mode runner-up: cheapest alternative fleet config
        if scaling and len(scaling) > 1:
            alt = scaling[1]
            if alt.option.instance.name != best.instance.name:
                price_delta = (alt.fleet_cost_usd_h - fb.fleet_cost_usd_h) / max(alt.fleet_cost_usd_h, 1e-6)
                if price_delta > 0.05:
                    parts.append(
                        f"Beats {alt.replicas}× {alt.option.instance.name} "
                        f"by {price_delta * 100:.0f}% on total fleet cost."
                    )
    else:
        parts.append(f"{anchor.instance.name} runs the model with {strategy_phrase}.")

        # Throughput context (single-instance mode)
        util_pct = 100.0 * anchor.per_gpu_need_gb / anchor.instance.vram_gb
        if anchor.single_stream_tok_s > 0:
            if args.users > 1:
                actual_pu = per_user_tok_s(anchor.single_stream_tok_s, args.users)
                parts.append(
                    f"At {args.users} concurrent users, each user gets "
                    f"~{actual_pu:.0f} tok/s "
                    f"(ceiling {anchor.single_stream_tok_s:.0f} tok/s single-stream)."
                )
            else:
                parts.append(
                    f"Single-stream throughput ~{anchor.single_stream_tok_s:.0f} tok/s, "
                    f"{util_pct:.0f}% VRAM utilized."
                )

        # Why this instance over the runner-up?
        if runner_up is not None and runner_up.single_stream_tok_s > 0:
            price_delta = (runner_up.price_usd_h - anchor.price_usd_h) / runner_up.price_usd_h
            speed_delta = (anchor.single_stream_tok_s - runner_up.single_stream_tok_s) / max(
                runner_up.single_stream_tok_s, 1e-6
            )
            if price_delta > 0.10 and speed_delta > -0.20:
                parts.append(
                    f"Beats {runner_up.instance.name} by "
                    f"{price_delta * 100:.0f}% on price."
                )
            elif speed_delta > 0.20:
                parts.append(
                    f"Beats {runner_up.instance.name} by "
                    f"{speed_delta * 100:.0f}% on throughput."
                )
            elif speed_delta > 0.05:
                parts.append(
                    f"Slightly faster than {runner_up.instance.name} "
                    f"({speed_delta * 100:.0f}% more tok/s)."
                )

    # --- Caveats (apply to both modes) ------------------------------------ #
    util_pct = 100.0 * anchor.per_gpu_need_gb / anchor.instance.vram_gb
    caveats: list[str] = []
    if anchor.quant_warning:
        caveats.append(f"⚠ {args.quant} on {anchor.instance.gpu} uses software emulation "
                       f"(penalty already reflected in tok/s estimates)")
    if util_pct > 80:
        caveats.append(f"⚠ {util_pct:.0f}% VRAM used — limited headroom for "
                       f"longer contexts or higher concurrency")
    if anchor.shared_eligible and anchor.total_gpus == 1 and not is_fleet:
        caveats.append(
            f"could share GPU with up to {TIME_SLICE_REPLICAS} models "
            f"via time-slicing (set shared: true to amortize cost)"
        )
    if anchor.over_price_ceiling:
        caveats.append("over your --max-price ceiling — no cheaper option fits")
    if caveats:
        parts.append(" ".join(caveats[:2]))   # cap at 2 caveats

    return " ".join(parts)




def _diagnose_no_fit(
    model: ModelSpec,
    args:  argparse.Namespace,
    prices: dict[str, float] | None,
    C:     type,
) -> None:
    """Explain WHY nothing fits and, crucially, what config WOULD — so the user
    gets a concrete re-run instead of a dead end. `best is None` means no
    instance can hold the model at all (VRAM/host-RAM), independent of price."""
    from .catalog import INSTANCES

    prices = prices or {}
    need = estimate_vram(model, args.quant, args.kv_quant, args.seq, args.batch, 1,
                         avg_context_len=args.avg_context).total_gb
    largest = max(INSTANCES, key=lambda i: i.vram_gb * i.num_gpus)
    largest_total = largest.vram_gb * largest.num_gpus

    tags = []
    if any("multi-modal" in w for w in model.warnings):
        tags.append("multi-modal")
    if model.is_moe:
        tags.append(f"MoE {model.num_experts}E")
    tag_str = f" ({', '.join(tags)})" if tags else ""

    print(f"\n  {C.BOLD}Model:{C.RESET} {model.model_id} — "
          f"{_fmt_params(model.params)} params{tag_str}, {model.num_layers} layers")
    print(f"  {C.BOLD}Needs:{C.RESET} {need:,.0f} GB VRAM "
          f"({C.CYAN}{args.quant}{C.RESET} weights, {args.seq:,}-token context)")
    gap = need - largest_total
    largest_line = (f"  {C.BOLD}Largest node:{C.RESET} {largest.name} — "
                    f"{largest.num_gpus}× {largest.gpu} = {largest_total:,.0f} GB")
    if gap > 0:
        print(f"{largest_line}  {C.RED}→ short by ~{gap:,.0f} GB{C.RESET}")
    else:
        print(f"{largest_line}  {C.YELLOW}→ has room; blocked by host RAM or "
              f"a parallelism constraint{C.RESET}")
    print(f"  {C.DIM}Each model serves on one node (≤8 GPUs). The llm-d tier adds "
          f"replicas for throughput, not model sharding — so multiple instances "
          f"won't fit a bigger model; multi-node model parallelism is out of "
          f"scope. Quantize or pick a smaller checkpoint.{C.RESET}")

    # What WOULD fit — try the highest-leverage knobs and report the cheapest
    # fitting instance for each (ignoring the price ceiling: fit first).
    def _fits(quant: str, seq: int):
        v = estimate_vram(model, quant, args.kv_quant, seq, args.batch, 1,
                          avg_context_len=min(args.avg_context or seq // 4, seq))
        opts = find_options(
            total_need_gb=v.total_gb, num_heads=model.num_heads, tp_pin=None,
            require_in_cluster=args.in_cluster_only, safety_margin=args.safety_margin,
            prices=prices, max_price=1e12, model=model, weight_q=quant,
            kv_q=args.kv_quant, users=1,
        )
        return (opts[0] if opts else None), v.total_gb

    trials: list[tuple[str, str, int]] = []          # (flags, quant, seq)
    if args.quant != "int4":
        trials.append(("--quant int4", "int4", args.seq))
    if args.seq > 4096:
        trials.append(("--quant int4 --seq 4096", "int4", 4096))

    shown: list[tuple[str, "Option", float]] = []
    for flags, quant, seq in trials:
        opt, gb = _fits(quant, seq)
        if opt is not None:
            shown.append((flags, opt, gb))

    if shown:
        print(f"\n  {C.BOLD}What would fit:{C.RESET}")
        for flags, o, gb in shown:
            strat = f", {o.parallelism_label}" if o.parallelism_label else ""
            print(f"    {C.GREEN}✓{C.RESET} {flags:<26} {C.DIM}→{C.RESET} {gb:,.0f} GB "
                  f"{C.DIM}→{C.RESET} {o.instance.name} "
                  f"({o.total_gpus}× {o.instance.gpu}{strat})  "
                  f"{C.BOLD}${o.price_usd_h:.2f}/hr{C.RESET}")
        print(f"\n  {C.BOLD}Re-run:{C.RESET} {C.CYAN}./platformctl new-model "
              f"{model.model_id} {shown[0][0]}{C.RESET}")
    else:
        print(f"\n  {C.YELLOW}Even int4 on the largest node ({largest_total:,.0f} GB) "
              f"can't hold this model.{C.RESET} It needs multi-node serving, which "
              f"this single-node platform doesn't cover — or point at a smaller / "
              f"already-quantized checkpoint.")
    print()


def _int4_savings_hint(
    model: ModelSpec,
    best:  "Option",
    args:  argparse.Namespace,
    prices: dict[str, float] | None,
    C:     type,
) -> str | None:
    """If switching to int4 would let a *cheaper* instance host the model, return
    a concrete one-liner (instance + price + % saved). Returns None when the user
    is already on int4-class weights or int4 wouldn't change the instance — so we
    never claim savings that don't exist."""
    if args.quant not in ("bf16", "fp16", "fp32"):
        return None
    prices = prices or {}
    v = estimate_vram(model, "int4", args.kv_quant, args.seq, args.batch, 1,
                      avg_context_len=args.avg_context)
    opts = find_options(
        total_need_gb=v.total_gb, num_heads=model.num_heads, tp_pin=None,
        require_in_cluster=args.in_cluster_only, safety_margin=args.safety_margin,
        prices=prices, max_price=1e12, model=model, weight_q="int4",
        kv_q=args.kv_quant, users=1,
    )
    if not opts:
        return None
    alt = opts[0]
    if alt.price_usd_h >= best.price_usd_h - 1e-6:
        return None   # int4 lands on the same/pricier GPU — no real saving
    save_pct = (best.price_usd_h - alt.price_usd_h) / best.price_usd_h * 100
    saved_mo = _fmt_monthly(best.price_usd_h - alt.price_usd_h)
    same_gpu = alt.instance.name == best.instance.name
    where = "on the same GPU" if same_gpu else f"on {alt.instance.name} ({alt.instance.gpu})"
    return (f"{C.CYAN}--quant int4{C.RESET} fits {where} at "
            f"{C.BOLD}${alt.price_usd_h:.2f}/hr{C.RESET} — {save_pct:.0f}% cheaper "
            f"(~{saved_mo}/mo saved){C.DIM}; ~1-2% quality loss on benchmarks{C.RESET}")


def _deploy_flags(args: argparse.Namespace) -> str:
    """Reconstruct the non-default flags for a faithful `--deploy` re-run line."""
    parts: list[str] = []
    if args.quant != "bf16":
        parts.append(f"--quant {args.quant}")
    if args.kv_quant != "fp16":
        parts.append(f"--kv-quant {args.kv_quant}")
    if args.seq != 8192:
        parts.append(f"--seq {args.seq}")
    if args.workload:
        parts.append(f"--workload {args.workload}")
    elif args.users > 1:
        parts.append(f"--users {args.users}")
    if getattr(args, "tier", "auto") not in (None, "auto"):
        parts.append(f"--tier {args.tier}")
    return (" " + " ".join(parts)) if parts else ""


def print_human(
    model:       ModelSpec,
    vram:        VramEstimate,
    opts:        list[Option],
    best:        Option | None,
    args:        argparse.Namespace,
    price_src:   str,
    scaling:     list[ScalingRecommendation] | None = None,
    prices:      dict[str, float] | None = None,
) -> None:
    C      = _palette(_should_use_colour(args))
    ruler  = _ruler(71, C)

    # -------- 1. Recommendation banner ----------------------------------- #
    if best is None:
        print(f"\n  {C.RED}✗ No catalog instance fits {model.model_id} as configured.{C.RESET}")
        _diagnose_no_fit(model, args, prices, C)
        return

    util_pct  = 100.0 * best.per_gpu_need_gb / best.instance.vram_gb
    marker    = f"{C.YELLOW}⚠{C.RESET}" if best.over_price_ceiling else f"{C.GREEN}✓{C.RESET}"
    verdict   = f"{C.YELLOW}FITS, BUT OVER BUDGET{C.RESET}" if best.over_price_ceiling \
                else f"{C.GREEN}RECOMMENDED{C.RESET}"
    # Fleet mode: show fleet-aware banner. Otherwise: single-instance banner.
    fleet_best = scaling[0] if scaling else None
    is_fleet_mode = args.target_users is not None and fleet_best is not None

    # In fleet mode, anchor labels to the fleet pick (which may differ from
    # the single-instance `best` in instance type AND parallelism strategy).
    banner_opt = fleet_best.option if is_fleet_mode else best
    shared_on = (not is_fleet_mode) and banner_opt.shared_eligible and banner_opt.total_gpus == 1
    if shared_on:
        mode_lbl = f"shared mode (up to {TIME_SLICE_REPLICAS} models per GPU)"
    elif banner_opt.parallelism_label:
        mode_lbl = banner_opt.parallelism_label
    else:
        mode_lbl = "dedicated GPU"

    print()
    print(f"  {C.DIM}Mode:{C.RESET} {C.BOLD}{_mode_line(args)}{C.RESET}")
    print(ruler)

    if is_fleet_mode:
        # Headline = fleet recommendation, not single-instance
        fb = fleet_best
        fleet_verdict = (
            f"{C.YELLOW}FLEET DOES NOT MEET SLO{C.RESET}" if fb.slo_unmet
            else (f"{C.GREEN}RECOMMENDED FLEET (SLO-capped){C.RESET}" if fb.slo_capped
                  else f"{C.GREEN}RECOMMENDED FLEET{C.RESET}")
        )
        fleet_marker = f"{C.YELLOW}⚠{C.RESET}" if fb.slo_unmet else f"{C.GREEN}✓{C.RESET}"
        print(f"  {fleet_marker} {C.BOLD}{fleet_verdict}: "
              f"{fb.replicas}× {fb.option.instance.name}{C.RESET} — "
              f"{fb.option.total_gpus}× NVIDIA {fb.option.instance.gpu} per replica · {mode_lbl}")
        fleet_monthly = _fmt_monthly(fb.fleet_cost_usd_h)
        r_word = "replica" if fb.replicas == 1 else "replicas"
        print(f"    {C.BOLD}${fb.fleet_cost_usd_h:.2f}/hr fleet{C.RESET}  ·  "
              f"~{fleet_monthly}/month  ({C.DIM}${fb.option.price_usd_h:.2f}/hr × "
              f"{fb.replicas} {r_word}{C.RESET})")
        slo_tone = C.GREEN if fb.estimated_tok_s_per_user >= args.target_tok_s else C.YELLOW
        print(f"    Capacity:    {fb.replicas} × {fb.effective_capacity} concurrent users "
              f"= {fb.replicas * fb.effective_capacity} total"
              f"{' (' + C.DIM + 'SLO-capped' + C.RESET + ')' if fb.slo_capped else ''}")
        print(f"    Throughput:  {slo_tone}~{fb.estimated_tok_s_per_user:.0f} tok/s/user "
              f"@ {fb.effective_capacity} concurrent per replica{C.RESET} "
              f"{C.DIM}(SLO {args.target_tok_s:.0f}; ceiling "
              f"{fb.option.single_stream_tok_s:.0f} tok/s){C.RESET}")
        if best.quant_warning:
            print(f"    {C.YELLOW}⚠ Quant compatibility: {best.quant_warning}{C.RESET}")
    else:
        # Single-instance banner (the original behaviour)
        print(f"  {marker} {C.BOLD}{verdict}: {best.instance.name}{C.RESET} — "
              f"{best.total_gpus}× NVIDIA {best.instance.gpu}, "
              f"{best.instance.vram_gb} GB VRAM per GPU · {mode_lbl}")
        monthly = _fmt_monthly(best.price_usd_h)
        line    = f"    {C.BOLD}${best.price_usd_h:.2f}/hr{C.RESET}  ·  ~{monthly}/month"
        if shared_on:
            eff   = _fmt_monthly(best.price_usd_h / TIME_SLICE_REPLICAS)
            line += (f"  ·  ~{eff}/month per model "
                     f"if {TIME_SLICE_REPLICAS} share the GPU")
        print(line)
        headroom_tone = C.GREEN if util_pct < 60 else (C.YELLOW if util_pct < 85 else C.RED)
        print(f"    Utilisation: {headroom_tone}{best.per_gpu_need_gb:.1f} / "
              f"{best.instance.vram_gb} GB ({util_pct:.0f}%){C.RESET}  "
              f"— {best.headroom_gb:.1f} GB headroom")
        if best.single_stream_tok_s > 0:
            actual_per_user = per_user_tok_s(best.single_stream_tok_s, args.users)
            per_user_label = (
                f"~{actual_per_user:.0f} tok/s/user @ {args.users} concurrent"
                if args.users > 1 else
                f"~{best.single_stream_tok_s:.0f} tok/s single-stream"
            )
            # Show effective bandwidth (after parallelism overhead) rather than
            # raw HBM × GPUs — the latter misleads when TP/PP overhead is high.
            eff_bw = best.instance.hbm_bandwidth_tb_s * best.tp_degree
            if best.total_gpus > 1:
                eff_bw *= _parallelism_efficiency(
                    best.tp_degree, best.pp_degree, best.instance.has_nvlink)
            print(f"    Throughput:  {per_user_label} "
                  f"{C.DIM}({best.instance.gpu}, "
                  f"{eff_bw:.2f} TB/s effective){C.RESET}")
        if best.quant_warning:
            print(f"    {C.YELLOW}⚠ Quant compatibility: {best.quant_warning}{C.RESET}")
        if best.over_price_ceiling:
            print(f"    {C.YELLOW}Note: exceeds --max-price ${args.max_price:.2f}/hr. "
                  f"No cheaper option fits.{C.RESET}")
    print(ruler)

    # -------- 1b. Why this recommendation? ------------------------------- #
    rationale = _explain_recommendation(best, opts, args, model, scaling)
    if rationale:
        print()
        print(f"  {C.BOLD}Why:{C.RESET} {rationale}")
    if not is_fleet_mode:
        hint = _int4_savings_hint(model, best, args, prices, C)
        if hint:
            print(f"  {C.BOLD}💡 Cheaper:{C.RESET} {hint}")
    if getattr(args, "_preset_applied", None):
        print(f"  {C.DIM}Workload preset '{args.workload}' applied: "
              f"{', '.join(args._preset_applied)}{C.RESET}")

    # -------- 2. MoE callout (if applicable) ----------------------------- #
    if model.is_moe and model.num_experts:
        active = model.active_experts or 0
        act_ratio = active / model.num_experts if model.num_experts else 0
        dense_eq  = int(model.params * act_ratio) if active else model.params
        print()
        print(f"  {C.YELLOW}⚠ MoE:{C.RESET} {model.num_experts} experts, {active} active/token — "
              f"all {_fmt_params(model.params)} reside in VRAM; "
              f"bandwidth ≈ {_fmt_params(dense_eq)} active.")

    # -------- 3. Model + request summary (compact) ----------------------- #
    gqa = f"GQA {model.num_kv_heads}KV/{model.num_heads}Q" \
          if model.num_kv_heads and model.num_kv_heads != model.num_heads else \
          f"MHA {model.num_heads} heads"
    display_users = args.users
    print(f"\n{C.BOLD}Model:{C.RESET}   {model.model_id}  "
          f"({C.DIM}{_fmt_params(model.params)} params, {model.num_layers} layers, "
          f"{gqa}{C.RESET})")
    print(f"{C.BOLD}Request:{C.RESET} {args.seq:,}-token context · "
          f"{display_users} concurrent · weights {args.quant} · kv {args.kv_quant}"
          f"{C.DIM}  (prices: {price_src}){C.RESET}")
    if "⚠" in price_src:
        print(f"  {C.YELLOW}⚠ Pricing fallback: catalog prices may not reflect "
              f"{args.region}. Use --refresh-prices with valid AWS credentials "
              f"for accurate pricing.{C.RESET}")
    for w in model.warnings:
        print(f"  {C.YELLOW}⚠{C.RESET} {w}")

    # -------- 4. VRAM breakdown (verbose only) --------------------------- #
    if args.verbose:
        print(f"\n{C.BOLD}VRAM usage{C.RESET} (TP=1, per-GPU before sharding)")
        total = vram.total_gb or 1e-9
        rows  = [
            ("weights",     vram.weights_gb),
            ("kv-cache",    vram.kv_cache_gb),
            ("activations", vram.activations_gb),
            ("overhead",    vram.overhead_gb),
        ]
        for label, gb in rows:
            pct = 100.0 * gb / total
            print(f"  {label:<12}{_bar(gb, total, 24)}  "
                  f"{gb:>6.2f} GB  ({pct:>4.1f}%)")
        print(f"  {C.BOLD}{'total':<12}{_bar(total, total, 24)}  "
              f"{total:>6.2f} GB{C.RESET}")

    # -------- 5. Alternatives table -------------------------------------- #
    # Dedupe: same GPU + strategy → only show cheapest variant.
    def _dedupe_key(o: Option) -> tuple:
        return (o.instance.gpu, o.instance.num_gpus, o.tp_degree, o.pp_degree)

    seen_keys: set[tuple] = set()
    deduped_opts: list[Option] = []
    for o in opts:
        k = _dedupe_key(o)
        if k in seen_keys:
            continue
        seen_keys.add(k)
        deduped_opts.append(o)

    # In fleet mode, skip the single-instance table (fleet alternatives
    # are shown below and the single-instance table sends mixed signals).
    if not is_fleet_mode:
        within_budget = [o for o in deduped_opts if not o.over_price_ceiling
                         and o.quant_warning is None]
        over_budget   = [o for o in deduped_opts if o.over_price_ceiling
                         or o.quant_warning is not None]

        # In default (non-verbose) mode, hide options that are more expensive
        # but no faster than a cheaper option — they only add VRAM headroom,
        # which isn't useful unless you need it (higher concurrency / longer
        # context). The user can see them with --verbose.
        if not args.verbose and best is not None:
            useful: list[Option] = []
            best_tok_s_so_far = 0.0
            for o in within_budget:
                o_tok_s = per_user_tok_s(o.single_stream_tok_s, args.users)
                if o_tok_s > best_tok_s_so_far or o is best:
                    useful.append(o)
                    best_tok_s_so_far = max(best_tok_s_so_far, o_tok_s)
            within_budget = useful

        # Default: show top 4. With --verbose, show the full table.
        display_limit = args.limit if args.verbose else min(4, args.limit)

        print(f"\n{C.BOLD}Alternatives{C.RESET} "
              f"{C.DIM}(--verbose for full table){C.RESET}\n")
        header = (f"  {'INSTANCE':<15} {'GPU':<9} {'STRATEGY':<10} "
                  f"{'VRAM':<12} {'TOK/S':>6}  {'$/HR':>7}  {'MONTHLY':>9}  NOTE")
        print(f"{C.DIM}{header}{C.RESET}")
        print(f"{C.DIM}  {'-'*15} {'-'*9} {'-'*10} "
              f"{'-'*12} {'-'*6}  {'-'*7}  {'-'*9}  {'-'*10}{C.RESET}")

        for o in within_budget[:display_limit]:
            _print_table_row(o, C, over=False, users=args.users)
        if args.verbose and over_budget:
            remaining = display_limit - len(within_budget[:display_limit])
            if remaining > 0:
                for o in over_budget[:remaining]:
                    _print_table_row(o, C, over=True, users=args.users)

    # -------- 6. Fleet scaling (when auto-fleet is triggered) ------------ #
    if scaling:
        _print_scaling_section(scaling, args, C)
        if prices is not None and scaling[0] is not None:
            _print_cost_levers(args, model, scaling[0], prices, C)

    # -------- 7. YAML artifact + next steps ------------------------------ #
    _print_yaml_snippet(model, vram, best, args, C, scaling)

    # -------- 7b. Next steps suggestions --------------------------------- #
    suggestions = _next_steps_lines(args, best, scaling)
    if suggestions:
        print(f"\n{C.BOLD}Next steps:{C.RESET}")
        for s in suggestions:
            print(f"  • {s}")

    # -------- 8. Footnotes (one line) ------------------------------------ #
    print(f"\n{C.DIM}Throughput estimates ±25% — validate with `vllm bench serve`. "
          f"Monthly = hourly × 730h. Prices: {price_src}{C.RESET}")


def _print_table_row(o: Option, C: type, over: bool, users: int = 1) -> None:
    strategy = o.parallelism_label or "1 GPU"
    usage  = f"{o.per_gpu_need_gb:.0f}/{o.instance.vram_gb} GB"
    tok_s  = (f"{per_user_tok_s(o.single_stream_tok_s, users):.0f}"
              if o.single_stream_tok_s > 0 else "?")
    price  = f"${o.price_usd_h:.2f}"
    month  = _fmt_monthly(o.price_usd_h)

    # Only flag exceptional states — shared eligibility or issues.
    note_parts: list[str] = []
    if over:
        note_parts.append(f"{C.YELLOW}over budget{C.RESET}")
    if o.shared_eligible:
        amort = o.amortised_price_usd_h
        if amort is not None:
            note_parts.append(f"shared ok ({C.DIM}${amort:.2f}/hr ea{C.RESET})")
    if o.quant_warning:
        note_parts.append(f"{C.YELLOW}⚠ quant{C.RESET}")
    if not o.in_cluster:
        note_parts.append(f"{C.DIM}out-of-cluster{C.RESET}")
    note = " · ".join(note_parts) if note_parts else ""

    print(f"  {o.instance.name:<15} {o.instance.gpu:<9} {strategy:<10} "
          f"{usage:<12} {tok_s:>6}  {price:>7}  {month:>9}  {note}")


def _compute_alt_fleet_cost(
    model:          ModelSpec,
    weight_q:       str,
    kv_q:           str,
    seq_len:        int,
    avg_context:    int,
    target_users:   int,
    target_tok_s:   float,
    utilization:    float,
    safety_margin:  float,
    prices:         dict[str, float],
    max_price:      float,
    num_heads:      int,
) -> "ScalingRecommendation | None":
    """Compute the cheapest fleet for an alternative parameter set.

    Used by the cost-lever feature to show 'what if I switched to int4?' or
    'what if I lowered the SLO?' impact in actual dollars.
    """
    vram_alt = estimate_vram(
        model, weight_q, kv_q, seq_len, batch_size=1,
        users=1, avg_context_len=avg_context,
    )
    opts_alt = find_options(
        total_need_gb=vram_alt.total_gb,
        num_heads=num_heads,
        tp_pin=None,
        require_in_cluster=False,
        safety_margin=safety_margin,
        prices=prices,
        max_price=max_price,
        model=model,
        weight_q=weight_q,
        kv_q=kv_q,
        users=1,
    )
    if not opts_alt:
        return None
    scaling_alt = compute_scaling(
        opts=opts_alt,
        vram=vram_alt,
        target_users=target_users,
        utilization_factor=utilization,
        safety_margin=safety_margin,
        target_tok_s=target_tok_s,
    )
    if not scaling_alt:
        return None
    # Pick the first non-SLO-unmet option
    for s in scaling_alt:
        if not s.slo_unmet:
            return s
    return scaling_alt[0]


def _print_cost_levers(
    args:        argparse.Namespace,
    model:       ModelSpec,
    fleet_best:  "ScalingRecommendation",
    prices:      dict[str, float],
    C:           type,
) -> None:
    """Print 'what if?' alternatives showing how cost changes with knob tweaks.

    Only runs in fleet mode. Computes 2-3 alternative fleet configurations
    by re-evaluating with one parameter tweaked at a time, showing the user
    where the cost-quality / cost-latency / cost-context trade-offs are.
    """
    base_cost = fleet_best.fleet_cost_usd_h
    levers: list[tuple[str, float, str, str]] = []   # (label, alt_cost, alt_instance, note)

    common_kwargs = dict(
        model=model,
        kv_q=args.kv_quant,
        seq_len=args.seq,
        target_users=args.target_users,
        utilization=args.utilization,
        safety_margin=args.safety_margin,
        prices=prices,
        max_price=999999.0,  # cost levers explore all options regardless of user's ceiling
        num_heads=model.num_heads,
    )

    avg_ctx = args.avg_context if args.avg_context else min(1024, args.seq)

    # Lever 1: switch to int4 (only suggest if not already int4-class)
    if args.quant in ("bf16", "fp16", "fp32"):
        alt = _compute_alt_fleet_cost(
            weight_q="int4",
            avg_context=avg_ctx,
            target_tok_s=args.target_tok_s,
            **common_kwargs,
        )
        if alt is not None:
            levers.append(("--quant int4", alt.fleet_cost_usd_h,
                           f"{alt.replicas}× {alt.option.instance.name}",
                           "1-2% quality loss on benchmarks"))

    # Lever 2: relax SLO by 40%
    if args.target_tok_s >= 10:
        new_slo = round(args.target_tok_s * 0.6)
        alt = _compute_alt_fleet_cost(
            weight_q=args.quant,
            avg_context=avg_ctx,
            target_tok_s=float(new_slo),
            **common_kwargs,
        )
        if alt is not None and alt.fleet_cost_usd_h < base_cost:
            levers.append((f"--target-tok-s {new_slo}", alt.fleet_cost_usd_h,
                           f"{alt.replicas}× {alt.option.instance.name}",
                           "lower per-user latency target"))

    # Lever 3: cap context at 256 (if not already short)
    if avg_ctx > 256:
        alt = _compute_alt_fleet_cost(
            weight_q=args.quant,
            avg_context=256,
            target_tok_s=args.target_tok_s,
            **common_kwargs,
        )
        if alt is not None and alt.fleet_cost_usd_h < base_cost:
            levers.append(("--avg-context 256", alt.fleet_cost_usd_h,
                           f"{alt.replicas}× {alt.option.instance.name}",
                           "for short Q&A workloads"))

    if not levers:
        return

    print(f"\n{C.BOLD}Cost levers{C.RESET} {C.DIM}(impact on monthly fleet cost){C.RESET}")
    for label, alt_cost, alt_instance, note in levers:
        delta = (alt_cost - base_cost) / base_cost * 100
        sign = "+" if delta > 0 else ""
        alt_monthly = alt_cost * 730
        inst_note = f" on {alt_instance}" if alt_instance else ""
        print(f"  {label:<22}  ${alt_monthly:>7,.0f}/mo "
              f"{C.DIM}({sign}{delta:.0f}%{inst_note}){C.RESET}  "
              f"{C.DIM}— {note}{C.RESET}")


def _print_scaling_section(
    scaling: list[ScalingRecommendation],
    args:    argparse.Namespace,
    C:       type,
) -> None:
    best_s = scaling[0] if scaling else None
    if not best_s:
        return

    ruler = _ruler(71, C)
    print(f"\n{ruler}")
    slo_label = (
        f"≥{args.target_tok_s:.0f} tok/s/user SLO, "
        if args.target_tok_s > 0 else ""
    )
    print(f"  {C.BOLD}FLEET ALTERNATIVES{C.RESET} — {args.target_users} target users, "
          f"{slo_label}{int(args.utilization * 100)}% util headroom")
    print(ruler)

    # Recommended fleet details (the headline is in the top banner now;
    # this section is for per-instance detail and alternatives table).
    strat = best_s.option.parallelism_label
    strat_info = f" ({strat})" if strat else ""
    print(f"\n  {C.DIM}Selected: {best_s.replicas}× {best_s.option.instance.name} "
          f"({best_s.option.instance.gpu}{strat_info}) — "
          f"max VRAM concurrency {best_s.max_concurrency_per_inst}, "
          f"total GPUs {best_s.replicas * best_s.option.total_gpus}{C.RESET}")

    if len(scaling) > 1:
        # Dedupe by (gpu, num_gpus, tp_degree, pp_degree) — same logic as the
        # single-instance alternatives table.
        seen: set[tuple] = set()
        deduped_scaling: list[ScalingRecommendation] = []
        for s in scaling:
            k = (s.option.instance.gpu, s.option.instance.num_gpus,
                 s.option.tp_degree, s.option.pp_degree)
            if k in seen:
                continue
            seen.add(k)
            deduped_scaling.append(s)

        print(f"\n  {C.BOLD}Fleet alternatives{C.RESET} "
              f"(sorted by total fleet cost; ⚠ = SLO unmet; "
              f"{C.DIM}duplicates by GPU+strategy hidden{C.RESET})\n")
        header = (f"    {'INSTANCE':<15} {'GPU':<9} {'STRATEGY':<10} "
                  f"{'CONC/INST':>9}  {'REPLICAS':>8}  {'TOK/S':>6}  "
                  f"{'FLEET $/HR':>10}  {'FLEET/MO':>9}  {'NODEPOOL':<14}")
        print(f"{C.DIM}{header}{C.RESET}")
        print(f"{C.DIM}    {'-'*15} {'-'*9} {'-'*10} "
              f"{'-'*9}  {'-'*8}  {'-'*6}  {'-'*10}  {'-'*9}  {'-'*14}{C.RESET}")

        for s in deduped_scaling[: args.limit]:
            tone = C.GREEN if s is best_s else (C.YELLOW if s.slo_unmet else "")
            reset = C.RESET if tone else ""
            marker = "→ " if s is best_s else ("⚠ " if s.slo_unmet else "  ")
            s_strat = s.option.parallelism_label or "1 GPU"
            tok_s_str = f"{s.estimated_tok_s_per_user:.0f}" if s.estimated_tok_s_per_user > 0 else "?"
            suffix = ""
            if s.option.quant_warning:
                suffix = f" {C.YELLOW}⚠ quant{C.RESET}"
            print(f"  {tone}{marker}{s.option.instance.name:<15} "
                  f"{s.option.instance.gpu:<9} {s_strat:<10} "
                  f"{s.effective_capacity:>9}  {s.replicas:>8}  "
                  f"{tok_s_str:>6}  "
                  f"${s.fleet_cost_usd_h:>9.2f}  "
                  f"{_fmt_monthly(s.fleet_cost_usd_h):>9}  "
                  f"{s.option.nodepool:<14}{suffix}{reset}")


def model_name_from_id(model_id: str) -> str:
    """Derive the kebab-case name used as the YAML filename, metadata.name, and
    LiteLLM model name. Single source of truth for deploy + undeploy."""
    return model_id.split("/")[-1].lower().replace(".", "-").replace("_", "-")


# --------------------------------------------------------------------------- #
# Serving-tier selection + per-kind manifest rendering                         #
# --------------------------------------------------------------------------- #
#
# Each tier is a different KRO CRD in front of the SAME vLLM engine, so the
# sizing math is identical — only the emitted manifest (kind, a few field
# names, target directory) differs.
TIER_KIND = {"vllm": "VLLMEndpoint", "llm-d": "LLMDEndpoint",
             "llm-d-disagg": "LLMDDisaggEndpoint"}
# KIND_DIR (which directory each kind's manifest is written to) is imported from
# .paths — the single source of truth shared with gitops.py's --undeploy search.
# Workload -> llm-d EPP routing profile (LLMDEndpoint.spec.routingProfile).
# Shared-prefix / multi-turn workloads win most from prefix+KV-aware routing;
# high-volume independent workloads want queue (load) balancing.
ROUTING_PROFILE_BY_WORKLOAD = {
    "chat": "prefix", "rag": "prefix", "code": "prefix", "agentic": "prefix",
    "summarization": "throughput", "batch": "throughput",
}


def pick_tier(args, best, scaling) -> str:
    """Choose the serving CRD. Explicit --tier wins; otherwise a fleet (2+
    replicas) goes to the llm-d scale tier for KV/prefix/load-aware routing, and
    a single replica goes to plain vLLM. Pipeline parallelism forces vLLM (the
    LLMDEndpoint RGD is single-node tensor-parallel only)."""
    t = (getattr(args, "tier", "auto") or "auto")
    if t != "auto":
        return t
    pp = getattr(best, "pp_degree", 1) or 1
    if scaling and scaling[0].replicas >= 2 and pp <= 1:
        return "llm-d"
    return "vllm"


def pick_routing_profile(args) -> str:
    """llm-d routing profile from the workload preset (default: balanced)."""
    return ROUTING_PROFILE_BY_WORKLOAD.get(getattr(args, "workload", None) or "", "balanced")


# Fraction of round-robin replicas an llm-d fleet needs to hit the same SLO:
# KV/prefix-aware routing raises the cache hit rate and load-aware routing avoids
# hot replicas, so a shared-prefix workload needs fewer. Conservative first-order
# estimate (like the throughput model's ±25%) — validate with `vllm bench serve`.
ROUTING_EFFICIENCY = {"prefix": 0.75, "throughput": 0.90, "latency": 0.85, "balanced": 0.85}


def llmd_replicas(min_replicas: int, profile: str) -> int:
    """Replica count for an llm-d fleet after applying routing efficiency."""
    return max(2, math.ceil(min_replicas * ROUTING_EFFICIENCY.get(profile, 0.85)))


# Long prompts make prefill (compute-bound) contend with decode (bandwidth-bound)
# on the same replicas, hurting TTFT. Prefill/decode disaggregation splits them
# into separate pools. We recommend it above this typical-context threshold.
DISAGG_CONTEXT_THRESHOLD = 8192


# Heterogeneous prefill/decode nodepools only pay off at scale: a large model
# whose decode fleet is big enough to justify matching hardware to each phase's
# bottleneck (prefill→compute, decode→bandwidth) and to absorb the cross-node
# KV-transfer hop. Below this, both pools stay on the shared gpu-inference pool.
PD_SPLIT_MIN_PARAMS = 30_000_000_000


def disagg_context(args) -> int:
    """Typical per-request context used for the disaggregation decision."""
    return getattr(args, "avg_context", None) or (getattr(args, "seq", 0) // 4)


def should_disaggregate(args) -> bool:
    return disagg_context(args) >= DISAGG_CONTEXT_THRESHOLD


def resolve_kind(tier: str, args, scaling) -> str:
    """Map the chosen tier to a CRD kind. The llm-d tier auto-splits into
    aggregated (LLMDEndpoint) vs disaggregated (LLMDDisaggEndpoint): a
    long-context fleet benefits from prefill/decode disaggregation."""
    if tier == "llm-d":
        return "LLMDDisaggEndpoint" if (scaling and should_disaggregate(args)) else "LLMDEndpoint"
    return TIER_KIND[tier]


# HuggingFace architecture (config.json `architectures[0]`) -> vLLM
# `--tool-call-parser` id (vLLM v0.24.0 registered names). Ordered most-specific
# first (llama4 before llama, gemma4 before gemma). Only high-confidence,
# tool-capable families are listed; anything else returns "" so the model
# deploys chat-only — the operator can still set toolCallParser by hand. This is
# the ONE place that knows the mapping, and the choice is written into the
# generated manifest: visible in the preview, versioned in git, overridable.
_ARCH_TOOL_PARSERS: list[tuple[str, str]] = [
    ("gemma4", "gemma4"),
    ("llama4", "llama4_pythonic"),
    ("qwen3", "hermes"),
    ("qwen2", "hermes"),
    ("mistral", "mistral"),
    ("mixtral", "mistral"),
    ("granite", "granite"),
    ("llama", "llama3_json"),
]


def tool_call_parser_for(architecture: str) -> str:
    """Best-effort vLLM tool-call parser for a model architecture, or "" if none
    is known (deploy chat-only). Prefix match on the lowercased architecture,
    most specific first."""
    a = (architecture or "").lower()
    for prefix, parser in _ARCH_TOOL_PARSERS:
        if a.startswith(prefix):
            return parser
    return ""


def build_endpoint_yaml(
    kind: str,
    model: ModelSpec, vram: VramEstimate, best: Option,
    args: argparse.Namespace,
    scaling: list[ScalingRecommendation] | None = None,
) -> tuple[str, str, str, str]:
    """Build the serving manifest (VLLMEndpoint | LLMDEndpoint |
    LLMDDisaggEndpoint) for the recommended config. All three front the same vLLM
    engine, so the sizing is identical — only the CRD kind, a few field names,
    and the target directory differ. Returns (name, yaml_path, yaml_body,
    commit_msg); used for both the printed snippet and --deploy so they can't
    drift."""
    name = model_name_from_id(model.model_id)
    # Validate the two externally-derived values before they are interpolated
    # into the manifest that --deploy commits and ArgoCD applies. model_id goes
    # into `model: "..."` and `name` into `name:`/the filename; a value with a
    # quote or newline would otherwise break or inject YAML (GitOps injection).
    if not is_valid_hf_model_id(model.model_id):
        sys.exit(f"error: refusing to emit a manifest for an unsafe model id: {model.model_id!r} "
                 f"(expected a Hugging Face id like org/name).")
    if not is_valid_model_name(name):
        sys.exit(f"error: model id {model.model_id!r} derives an invalid Kubernetes name {name!r} "
                 f"(must be an RFC 1123 label). Rename or pick a different model.")
    max_len = args.seq
    if scaling:
        best = scaling[0].option

    is_disagg = kind == "LLMDDisaggEndpoint"
    is_llmd = kind == "LLMDEndpoint"
    is_llmd_family = is_llmd or is_disagg
    # shared time-slicing is a single-replica, non-llm-d feature.
    shared = (not scaling) and (not is_llmd_family) and best.total_gpus == 1 and best.shared_eligible

    directory  = KIND_DIR[kind]
    yaml_path  = f"{directory}/{name}.yaml"
    commit_msg = f"feat: deploy {name} ({kind})"

    if shared:
        min_vram_gib = max(1, math.ceil(best.per_gpu_need_gb * TIME_SLICE_REPLICAS * 1.10))
    else:
        min_vram_gib = max(1, math.ceil(best.per_gpu_need_gb * 1.15))
    worker_mem_gib = max(8, math.ceil(vram.weights_gb + 4))

    # Replica sizing — same math for every kind.
    if scaling:
        best_scaling = scaling[0]
        min_replicas = best_scaling.replicas
        max_replicas = max(min_replicas, math.ceil(min_replicas * 1.5))
        max_num_seqs = max(64, math.ceil(best_scaling.effective_capacity * 1.5))
    else:
        min_replicas = 1
        max_replicas = 2
        max_num_seqs = max(64, args.users * 2)

    lines: list[str] = [
        "apiVersion: kro.run/v1alpha1",
        f"kind: {kind}",
        "metadata:",
        f"  name: {name}",
        "  namespace: inference",
        "spec:",
        f'  model: "{model.model_id}"',
        f"  gpuCount: {best.total_gpus}",
    ]
    if best.tp_degree > 1:
        lines.append(f"  tensorParallelSize: {best.tp_degree}")
    # Pipeline parallelism: IE/vLLM only (the llm-d RGD is single-node TP).
    if best.pp_degree > 1 and not is_llmd_family:
        lines.append(f"  pipelineParallelSize: {best.pp_degree}")
    if shared:
        lines.append("  shared: true          # fits with headroom — up to 4 models share the GPU")

    lines.append(f"  maxModelLen: {max_len}")
    lines.append(f"  minVramPerGpuGiB: {min_vram_gib}")
    lines.append(f'  workerMemory: "{worker_mem_gib}Gi"')

    if is_disagg:
        # Disaggregated scale tier: prefill (compute-bound) and decode (KV-cache/
        # bandwidth-bound) autoscale independently on their own signals. Size the
        # *max* per pool from the target load — decode holds the sessions so it
        # gets the majority (~1/3 prefill : 2/3 decode); min stays 1 (elastic;
        # scale-to-zero parked).
        profile = pick_routing_profile(args)
        total = llmd_replicas(min_replicas, profile) if scaling else 3
        decode_max = max(1, math.ceil(total * 0.66))
        prefill_max = max(1, total - decode_max)
        lines.append("  prefillMinReplicas: 1")
        lines.append(f"  prefillMaxReplicas: {prefill_max}")
        lines.append("  prefillTargetQueueDepth: 20   # scale prefill on vLLM queue depth (compute-bound)")
        lines.append("  decodeMinReplicas: 1")
        lines.append(f"  decodeMaxReplicas: {decode_max}")
        lines.append("  decodeTargetKvCachePct: 85     # scale decode on KV-cache utilization (memory-bound)")
        lines.append("  pdDisaggregation: prefix-based # disaggregate only when the non-cached prompt suffix is long enough")
        lines.append(f"  routingProfile: {profile}   # EPP KV/prefix/load-aware weights for this workload")
        # Opt-in heterogeneous nodepools: worth it only for a large model with a
        # sustained multi-replica decode fleet (match prefill→compute /
        # decode→bandwidth hardware). Below the threshold, stay on the shared
        # gpu-inference pool (schema default) to avoid the cross-node KV hop.
        if model.params >= PD_SPLIT_MIN_PARAMS and decode_max >= 2:
            lines.append("  prefillNodePool: gpu-prefill   # compute-optimized (prefill is FLOP-bound)")
            lines.append("  decodeNodePool: gpu-decode     # bandwidth-optimized (decode is HBM-bound)")
    elif is_llmd:
        # Scale tier: KEDA autoscales the pool on vLLM queue depth and the EPP
        # routes across the live replicas. The recommender sizes the *peak*
        # (maxReplicas) for the target load; minReplicas stays at 1 for cost
        # (scale up on demand — raise it for a warm baseline). targetQueueDepth is
        # the per-pod scale trigger (matches the RGD default).
        profile = pick_routing_profile(args)
        peak = llmd_replicas(min_replicas, profile) if scaling else max(2, min_replicas)
        lines.append("  minReplicas: 1")
        lines.append(f"  maxReplicas: {peak}")
        lines.append("  targetQueueDepth: 20")
        lines.append(f"  routingProfile: {profile}   # EPP KV/prefix/load-aware weights for this workload")
    elif kind == "VLLMEndpoint":
        # vLLM is the fixed-size tier — no built-in autoscaler, so a single
        # replica count (not min/max). The llm-d tier is the scale path.
        lines.append(f"  # maxNumSeqs: {max_num_seqs}")
        lines.append(f"  replicas: {min_replicas}")

    # Tool/function calling applies to all three tiers (each fronts vLLM). Auto-
    # enable it for known tool-capable families so tool_choice:auto works out of
    # the box; written explicitly so it's visible and overridable (delete for
    # chat-only, or change if you pin a different vllmImage). Unknown families
    # emit nothing (chat-only).
    parser = tool_call_parser_for(model.architecture)
    if parser:
        lines.append(f"  toolCallParser: {parser}   # auto-detected from {model.architecture} — enables tool calling; remove for chat-only")

    yaml_body = "\n".join(lines) + "\n"
    return name, yaml_path, yaml_body, commit_msg


def _print_yaml_snippet(model: ModelSpec, vram: VramEstimate, best: Option,
                        args: argparse.Namespace, C: type,
                        scaling: list[ScalingRecommendation] | None = None) -> None:
    tier = pick_tier(args, best, scaling)
    kind = resolve_kind(tier, args, scaling)
    name, yaml_path, yaml_body, commit_msg = build_endpoint_yaml(
        kind, model, vram, best, args, scaling)

    why = {"LLMDDisaggEndpoint": "long-context fleet -> disaggregated llm-d (prefill/decode split + KV/prefix routing)",
           "LLMDEndpoint": "fleet of 2+ replicas -> llm-d scale tier (KV/prefix/load-aware routing)",
           "VLLMEndpoint": "single replica -> plain vLLM (simplest, no router)"}.get(kind, kind)
    print(f"\n{C.BOLD}Serving tier:{C.RESET} {C.BOLD}{kind}{C.RESET} {C.DIM}- {why}{C.RESET}")
    parser = tool_call_parser_for(model.architecture)
    if parser:
        print(f"{C.DIM}  tool calling: enabled — --tool-call-parser {parser} "
              f"(auto-detected from {model.architecture}; remove toolCallParser for chat-only).{C.RESET}")
    if kind in ("LLMDEndpoint", "LLMDDisaggEndpoint"):
        prof = pick_routing_profile(args)
        print(f"{C.DIM}  routingProfile '{prof}' baked into the manifest (EPP scorer weights).{C.RESET}")
        if kind == "LLMDDisaggEndpoint":
            print(f"{C.BOLD}  ⚑ disaggregated: separate prefill (KV producer) + decode (KV consumer) pools "
                  f"for long context (~{disagg_context(args)} tok).{C.RESET}")
            print(f"{C.DIM}    Runtime P/D needs a NIXL-enabled vLLM image.{C.RESET}")
        elif scaling:
            base = scaling[0].replicas
            eff = llmd_replicas(base, prof)
            if eff < base:
                pct = int((1 - ROUTING_EFFICIENCY.get(prof, 0.85)) * 100)
                print(f"{C.DIM}  routing efficiency (~{pct}% for '{prof}'): "
                      f"{eff} replicas vs {base} for round-robin vLLM.{C.RESET}")

    # --deploy/--undeploy do the git work for you; the copy-paste block is the
    # manual path. Point at the automated one so users know it exists.
    plural = {"VLLMEndpoint": "vllmendpoints",
              "LLMDEndpoint": "llmdendpoints", "LLMDDisaggEndpoint": "llmddisaggendpoints"}[kind]

    # Manifest preview — the "you'll deploy this" box.
    print(f"\n{C.BOLD}You'll deploy this{C.RESET} {C.DIM}→ {yaml_path}{C.RESET}")
    for ln in yaml_body.rstrip().splitlines():
        print(f"  {C.DIM}│{C.RESET} {ln}")

    # Primary path: the native one-liner (previews + confirms before it commits).
    flags = _deploy_flags(args)
    print(f"\n{C.BOLD}Deploy it:{C.RESET}  "
          f"{C.CYAN}./platformctl new-model {model.model_id}{flags} --deploy{C.RESET}")
    print(f"  {C.DIM}writes the manifest, commits, pushes, and triggers an ArgoCD "
          f"sync — you preview + confirm first.{C.RESET}")
    print(f"  {C.DIM}Remove it later:  ./platformctl new-model --undeploy {name}{C.RESET}")

    # Manual copy-paste path — only when asked (keeps the default tidy). The
    # heredoc uses a quoted 'EOF' so the YAML is not subject to shell expansion.
    if args.verbose:
        print(f"\n{C.DIM}Or by hand:{C.RESET}")
        print(f"cat > {yaml_path} <<'EOF'")
        print(yaml_body, end="")
        print("EOF")
        print(f'git add {yaml_path} && git commit -m "{commit_msg}" && git push')
        print(f"kubectl get {plural} -n inference -w   {C.DIM}# watch it come up{C.RESET}")


def print_json(
    model:     ModelSpec,
    vram:      VramEstimate,
    opts:      list[Option],
    best:      Option | None,
    args:      argparse.Namespace,
    price_src: str,
    scaling:   list[ScalingRecommendation] | None = None,
) -> None:
    payload = {
        "model": {
            "id":           model.model_id,
            "params":       model.params,
            "architecture": model.architecture,
            "num_layers":   model.num_layers,
            "hidden_size":  model.hidden_size,
            "num_heads":    model.num_heads,
            "num_kv_heads": model.num_kv_heads,
            "is_moe":       model.is_moe,
            "num_experts":  model.num_experts,
            "warnings":     model.warnings,
        },
        "request": {
            "weight_quant": args.quant,
            "kv_quant":     args.kv_quant,
            "seq_len":      args.seq,
            "batch_size":   args.batch,
            "users":        args.users,
            "tp_pin":       args.tp,
            "region":       args.region,
            "max_price":    args.max_price,
            "price_source": price_src,
        },
        "vram_gb": {
            "weights":     round(vram.weights_gb, 3),
            "kv_cache":    round(vram.kv_cache_gb, 3),
            "activations": round(vram.activations_gb, 3),
            "overhead":    round(vram.overhead_gb, 3),
            "total":       round(vram.total_gb, 3),
        },
        "options": [
            {
                "instance":             o.instance.name,
                "gpu":                  o.instance.gpu,
                "num_gpus":             o.instance.num_gpus,
                "vram_gb_per_gpu":      o.instance.vram_gb,
                "hbm_bandwidth_tb_s":   o.instance.hbm_bandwidth_tb_s,
                "compute_capability":   o.instance.compute_capability,
                "arch_family":          o.instance.arch_family,
                "tp_degree":            o.tp_degree,
                "pp_degree":            o.pp_degree,
                "per_gpu_need_gb":      round(o.per_gpu_need_gb, 3),
                "headroom_gb":          round(o.headroom_gb, 3),
                "single_stream_tok_s":  round(o.single_stream_tok_s, 1),
                "per_user_tok_s_at_users": round(o.per_user_tok_s_at_users, 1),
                "price_usd_h":          round(o.price_usd_h, 4),
                "monthly_usd":          round(o.price_usd_h * 730, 2),
                "nodepool":             o.nodepool,
                "shared_eligible":      o.shared_eligible,
                "over_price_ceiling":   o.over_price_ceiling,
                "quant_warning":        o.quant_warning,
                "notes":                o.notes,
            }
            for o in opts[: args.limit]
        ],
        "recommended": None if not best else {
            "instance":             best.instance.name,
            "tp_degree":            best.tp_degree,
            "pp_degree":            best.pp_degree,
            "total_gpus":           best.total_gpus,
            "nodepool":             best.nodepool,
            "shared_eligible":      best.shared_eligible,
            "single_stream_tok_s":  round(best.single_stream_tok_s, 1),
            "per_user_tok_s_at_users": round(best.per_user_tok_s_at_users, 1),
            "price_usd_h":          round(best.price_usd_h, 4),
            "monthly_usd":          round(best.price_usd_h * 730, 2),
            "over_price_ceiling":   best.over_price_ceiling,
            "quant_warning":        best.quant_warning,
        },
    }
    if scaling:
        best_s = scaling[0]
        payload["scaling"] = {
            "target_users":       args.target_users,
            "target_tok_s":       args.target_tok_s,
            "utilization_factor": args.utilization,
            "recommended": {
                "instance":                    best_s.option.instance.name,
                "tp_degree":                   best_s.option.tp_degree,
                "replicas":                    best_s.replicas,
                "max_concurrency_per_instance": best_s.max_concurrency_per_inst,
                "effective_capacity_per_inst":  best_s.effective_capacity,
                "fleet_capacity":              best_s.effective_capacity * best_s.replicas,
                "estimated_tok_s_per_user":    round(best_s.estimated_tok_s_per_user, 1),
                "slo_capped":                  best_s.slo_capped,
                "slo_unmet":                   best_s.slo_unmet,
                "fleet_cost_usd_h":            round(best_s.fleet_cost_usd_h, 4),
                "fleet_monthly_usd":           round(best_s.fleet_monthly_usd, 2),
                "total_gpus":                  best_s.replicas * best_s.option.total_gpus,
                "nodepool":                    best_s.option.nodepool,
            },
            "alternatives": [
                {
                    "instance":                    s.option.instance.name,
                    "tp_degree":                   s.option.tp_degree,
                    "replicas":                    s.replicas,
                    "max_concurrency_per_instance": s.max_concurrency_per_inst,
                    "effective_capacity_per_inst":  s.effective_capacity,
                    "estimated_tok_s_per_user":    round(s.estimated_tok_s_per_user, 1),
                    "slo_capped":                  s.slo_capped,
                    "slo_unmet":                   s.slo_unmet,
                    "fleet_cost_usd_h":            round(s.fleet_cost_usd_h, 4),
                    "fleet_monthly_usd":           round(s.fleet_monthly_usd, 2),
                    "nodepool":                    s.option.nodepool,
                }
                for s in scaling[: args.limit]
            ],
        }
    print(json.dumps(payload, indent=2))
