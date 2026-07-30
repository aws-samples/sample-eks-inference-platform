"""Fleet scaling: replica counts to serve N users, with optional per-user SLO."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .recommend import Option
from .throughput import per_user_tok_s
from .vram import VramEstimate


# --------------------------------------------------------------------------- #
# Fleet scaling                                                               #
# --------------------------------------------------------------------------- #

DEFAULT_UTILIZATION_FACTOR = 0.70  # leave 30% headroom for burst traffic
DEFAULT_TARGET_TOK_S       = 0.0   # disabled by default — only enforce when user explicitly sets SLO


# Workload presets — fill in sensible defaults for --avg-context, --target-tok-s,
# Workload presets fill sensible defaults for --avg-context, --target-tok-s,
# and --users when the user asks for one of these labels instead of having to
# tune individual flags. Users can still override any individual value.
#
# These are calibrated to typical production loads, not paper-spec scenarios.
# Fields:
#   avg_context  : input + output tokens per active sequence at steady state
#   target_tok_s : per-user decode rate that "feels right" for that workload
#   users        : default per-instance concurrency target (when --users not set)
#                  (applied when --users is at its default of 1)
WORKLOAD_PRESETS = {
    "chat": {
        "avg_context":  1024,   # short turns, ~500 input + ~500 output
        "target_tok_s": 25,     # interactive feel — 20-30 tok/s reads as snappy
        "users":         4,
        "description": "Interactive chat: short turns, snappy decode required",
    },
    "rag": {
        "avg_context":  4096,   # retrieved context blob + modest answer
        "target_tok_s": 15,     # users tolerate slower output for grounded responses
        "users":         8,
        "description": "Retrieval-augmented generation: long context, modest output",
    },
    "code": {
        "avg_context":  2048,   # surrounding file context + completion
        "target_tok_s": 35,     # IDE streaming — 30-40 tok/s feels responsive
        "users":         4,
        "description": "Code completion / IDE assistant: fast decode, streamed output",
    },
    "summarization": {
        "avg_context":  8192,   # long input documents, short summaries
        "target_tok_s": 10,     # latency tolerance is high for batch-y work
        "users":         8,
        "description": "Document summarization: long context, short output, modest latency",
    },
    "batch": {
        "avg_context":  1024,   # mixed prompts/outputs in offline pipelines
        "target_tok_s":  5,     # throughput dominates; latency doesn't matter much
        "users":        64,
        "description": "Offline batch inference: maximize throughput, latency irrelevant",
    },
}

@dataclass(frozen=True)
class ScalingRecommendation:
    option:                    Option
    max_concurrency_per_inst:  int       # VRAM-limited max concurrent sequences
    effective_capacity:        int       # after utilization factor (and SLO if applicable)
    replicas:                  int
    fleet_cost_usd_h:          float
    utilization_factor:        float
    target_users:              int
    target_tok_s:              float = 0.0
    # PR 3: estimated per-user tok/s at the effective capacity. If this is
    # below target_tok_s, we capped effective_capacity downward so the per-
    # instance load actually meets the SLO.
    estimated_tok_s_per_user:  float = 0.0
    slo_capped:                bool  = False
    slo_unmet:                 bool  = False  # True if even concurrency=1 misses SLO

    @property
    def fleet_monthly_usd(self) -> float:
        return self.fleet_cost_usd_h * 730

    @property
    def sort_key(self) -> tuple:
        # Sort by total fleet cost — what the user actually pays. A single
        # expensive instance (over per-instance ceiling) may still be the
        # cheapest fleet option because it serves more concurrent users.
        # SLO-unmet options sink to the bottom — they can't actually meet
        # the throughput requirement at any concurrency.
        return (
            self.slo_unmet,
            not self.option.in_cluster,
            self.fleet_cost_usd_h,
            self.replicas,
        )


def _max_concurrency_for_option(
    opt: Option,
    vram: VramEstimate,
    safety_margin: float,
) -> int:
    """Max concurrent sequences an instance can serve (VRAM-limited).

    Subtracts weights + activations + overhead from available VRAM, then divides
    by KV cache per sequence. This is the hard ceiling before vLLM starts queueing.

    With PP, each GPU holds 1/pp of the layers (weights AND KV cache); with TP,
    each GPU holds 1/tp of each layer's KV heads. The total KV cache per
    sequence is therefore divided by `total_sharding = tp × pp` when
    distributed across GPUs — which means a multi-GPU instance can hold
    `total_sharding ×` more concurrent sequences than a single GPU of the
    same model, beyond the obvious VRAM-budget gain.

    PR 5 — overhead consistency fix. Previously this function applied the 15%
    framework pad to (weights + activations) only, leaving KV cache un-padded.
    The fix applies the pad consistently across all three components, mirroring
    the `estimate_vram()` calculation. Algebra:
        avail = (1+pad) × (weights + activations + kv_budget)
        ⇒ kv_budget = avail/(1+pad) − weights − activations

    PR 7 (additional fix discovered during gemma-3-27b testing) — the per-seq
    KV cost was previously the full unsharded value, which heavily over-counted
    KV memory on PP/TP-sharded instances and made fleet recommendations
    massively over-provisioned (e.g. 13 replicas where 2 sufficed). KV cache
    in vLLM with vanilla TP/PP is sharded across the parallelism group, just
    like weights — so per-GPU per-seq KV = full_kv_per_seq / total_sharding.

    PR 8 (PagedAttention realism fix) — previously divided by `kv_per_seq_gb`
    (KV at FULL max_model_len), assuming every concurrent user fills the
    context cap. With vLLM's PagedAttention, KV is allocated per-token-in-flight
    rather than reserved up to max_model_len. Switching to `kv_per_active_seq_gb`
    (KV at the typical working context, default seq//4) brings concurrency math
    in line with measured behavior: in benchmarks, gemma-3-27b INT4 on a single
    L40S held 64 concurrent users at maxModelLen=4096 even though pre-fix v2
    would have predicted a hard cap of 16.
    """
    OVERHEAD_PAD = 0.15
    total_sharding = opt.total_gpus
    available_per_gpu = opt.instance.vram_gb * (1.0 - safety_margin)
    weights_per_gpu = vram.weights_gb / total_sharding
    activations_per_gpu = vram.activations_gb / total_sharding
    # Use the working-context KV (PagedAttention-realistic), not the
    # max-model-len worst case. Sharded across the TP/PP group.
    per_seq_kv = vram.kv_per_active_seq_gb if vram.kv_per_active_seq_gb > 0 else vram.kv_per_seq_gb
    kv_per_seq_per_gpu = per_seq_kv / total_sharding

    kv_budget = (
        available_per_gpu / (1.0 + OVERHEAD_PAD)
        - weights_per_gpu
        - activations_per_gpu
    )
    if kv_budget <= 0 or kv_per_seq_per_gpu <= 0:
        return 0
    return max(1, int(kv_budget / kv_per_seq_per_gpu))


def _slo_capacity(single_stream_tok_s: float, target_tok_s: float,
                  vram_capacity: int) -> tuple[int, bool, bool]:
    """Return (slo_capacity, was_capped, was_unmet) for a per-instance SLO.

    Finds the largest concurrency at which per_user_tok_s(N) >= target_tok_s.
    If even N=1 misses the SLO, returns (1, False, True) so callers know this
    instance type cannot meet the SLO at any load.
    """
    if target_tok_s <= 0 or single_stream_tok_s <= 0:
        return vram_capacity, False, False

    if single_stream_tok_s < target_tok_s:
        # Even single-stream is below SLO: this GPU is too slow. Caller
        # should escalate to a faster GPU.
        return 1, False, True

    # Walk down from vram_capacity until SLO is met. Cheaper than solving the
    # piecewise function analytically, and concurrency rarely exceeds 100.
    n = vram_capacity
    while n > 1 and per_user_tok_s(single_stream_tok_s, n) < target_tok_s:
        n -= 1
    return n, n < vram_capacity, False


def compute_scaling(
    opts:               list[Option],
    vram:               VramEstimate,
    target_users:       int,
    utilization_factor: float,
    safety_margin:      float,
    target_tok_s:       float = 0.0,
) -> list[ScalingRecommendation]:
    """For each viable option, compute how many replicas are needed to serve target_users.

    When target_tok_s > 0, the per-instance effective capacity is capped to
    the concurrency at which the per-user decode throughput meets the SLO.
    This means cheaper-but-slow GPUs require more replicas to hit the same SLO
    that an expensive-but-fast GPU clears on a single replica.
    """
    results: list[ScalingRecommendation] = []

    for opt in opts:
        max_conc = _max_concurrency_for_option(opt, vram, safety_margin)
        if max_conc < 1:
            continue

        # Step 1: VRAM-limited capacity, with utilization burst headroom.
        vram_capacity = max(1, int(max_conc * utilization_factor))

        # Step 2: SLO-limited capacity. Take the more restrictive of the two.
        slo_cap, slo_capped, slo_unmet = _slo_capacity(
            opt.single_stream_tok_s, target_tok_s, vram_capacity,
        )
        effective = min(vram_capacity, slo_cap) if target_tok_s > 0 else vram_capacity

        replicas = math.ceil(target_users / max(1, effective))
        fleet_cost = replicas * opt.price_usd_h

        # Compute the actual per-user tok/s the fleet would deliver at the
        # planned concurrency (effective capacity is the per-instance load).
        est_tok_s = (
            per_user_tok_s(opt.single_stream_tok_s, effective)
            if opt.single_stream_tok_s > 0 else 0.0
        )

        results.append(ScalingRecommendation(
            option=opt,
            max_concurrency_per_inst=max_conc,
            effective_capacity=effective,
            replicas=replicas,
            fleet_cost_usd_h=fleet_cost,
            utilization_factor=utilization_factor,
            target_users=target_users,
            target_tok_s=target_tok_s,
            estimated_tok_s_per_user=est_tok_s,
            slo_capped=slo_capped,
            slo_unmet=slo_unmet,
        ))

    results.sort(key=lambda r: r.sort_key)
    return results
