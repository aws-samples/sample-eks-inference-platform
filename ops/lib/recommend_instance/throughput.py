"""Bandwidth-aware decode throughput model (single-stream + concurrency decay)."""

from __future__ import annotations

import math

from .catalog import (
    TIME_SLICE_REPLICAS,
    WEIGHT_BYTES,
    Instance,
    _quant_compat_warning,
)
from .model import ModelSpec


# --------------------------------------------------------------------------- #
# Throughput model (PR 2)                                                     #
#                                                                             #
# Decode is HBM-bandwidth-bound: each generated token requires streaming the  #
# active model parameters from HBM through compute. Single-stream tok/s ≈     #
# kernel_efficiency × HBM_bandwidth / active_param_bytes. With concurrency,   #
# vLLM batches reads via continuous batching, so per-user tok/s decays as     #
# roughly 1/sqrt(N) until the saturation batch size, then ~1/N. Estimates are #
# first-order (±25%); validate with `vllm bench serve`.                       #
# --------------------------------------------------------------------------- #

# Empirical kernel efficiency factor — accounts for non-ideal HBM utilization,
# attention compute on KV cache (not just weight read), kernel launch overhead,
# and small allocator/fragmentation losses.
#
# Validated against vllm bench on AWQ INT4 gemma-3-27b on L40S (May 2026):
# measured ~0.56 effective bandwidth utilization, matching the int4 entry below.
#
# Lower for quantized formats (dequant overhead on the kernel path); higher for
# native dtypes (FP8 on Hopper hits very high MBU per NVIDIA spec).
DECODE_KERNEL_EFFICIENCY_BY_DTYPE = {
    "fp32":  0.70,
    "fp16":  0.65,
    "bf16":  0.65,   # Calibrated from real bench (A: gemma-3-1b on L4 = 0.65, B: SmolLM3-3B on L40S = 0.67)
    "fp8":   0.80,   # Hopper/Ada native fp8 — high MBU (not yet measured)
    "int8":  0.65,
    "int4":  0.55,   # Validated: AWQ INT4 gemma-3-27b on L40S measured 0.56
    "gptq4": 0.55,
    "awq4":  0.55,
    "nf4":   0.50,   # bitsandbytes 4-bit, slightly slower kernel
}

# Backwards-compat default (used if dtype not in table).
DECODE_KERNEL_EFFICIENCY = 0.70

# Multi-GPU parallelism overhead.
#
# Tensor parallelism does an all-reduce after every layer. With N layers and
# TP=k, that's 2(k-1)/k * N synchronization points per fwd pass — significant
# cost even on fast interconnects. Validated against scenario C:
#   gemma-3-12b (48 layers) on TP=4 over A100 NVLink → measured 0.45x of
#   per-GPU bandwidth roofline. v2 was previously treating TP=4 as 1.0x,
#   over-predicting throughput by 2-3x for multi-GPU dense models.
#
# Pipeline parallelism communicates less per step (only the activations
# crossing a stage boundary, not all-reduces). The cost is the pipeline
# "bubble" at start/end of each engine step. Much smaller penalty.
#
# These are first-order estimates calibrated to one measurement (C) plus
# vLLM blog guidance. Refine with more benchmarks across TP/PP/interconnect
# combinations.
TP_OVERHEAD_NVLINK = {1: 1.00, 2: 0.85, 4: 0.50, 8: 0.35}
TP_OVERHEAD_PCIE   = {1: 1.00, 2: 0.50, 4: 0.25, 8: 0.15}
# PP_OVERHEAD calibrated against PP=4 on A100 NVLink (gemma-3-12b, May 2026):
#   single-stream measured 35 tok/s vs raw bandwidth-roofline of 47 tok/s
#   → real PP=4 effective penalty 0.75 (was 0.90 too optimistic).
# At high concurrency (N=32), PP also under-performs the simple sqrt-decay
# model — pipeline bubbles + cross-stage activation transfers add real cost.
# Numbers below are first-order; refine with more PP test points across
# different stage counts and interconnects.
PP_OVERHEAD        = {1: 1.00, 2: 0.85, 4: 0.75, 8: 0.65}


def _parallelism_efficiency(tp: int, pp: int, has_nvlink: bool) -> float:
    """Combined TP + PP overhead multiplier vs the raw bandwidth roofline."""
    tp_table = TP_OVERHEAD_NVLINK if has_nvlink else TP_OVERHEAD_PCIE
    tp_eff = tp_table.get(tp, 0.20)
    pp_eff = PP_OVERHEAD.get(pp, 0.80)
    return tp_eff * pp_eff

# Concurrency curve: per-user tok/s = single_stream × decay(N).
#
# Below the saturation batch B_sat, throughput-per-user is roughly FLAT — vLLM's
# continuous batching amortizes weight reads across the entire batch "for free"
# in the bandwidth-bound regime. Empirically (gemma-3-27b INT4 on L40S, seq=4K)
# per-user tok/s drops only ~5% from N=1 to N=16, then ~10% by N=32, ~35% by N=64.
#
# Above B_sat, decay is sub-linear at 1/sqrt(N/B_sat) — continuous batching
# still provides amortization even at high concurrency, so degradation is
# gentler than the naive 1/N model. The formula is continuous at the crossover.
#
# B_sat=24 calibrated against scenarios A/B/D — measured per-user tok/s holds
# essentially flat through N=16, very gentle drop through N=32, then degrades
# above. Was 16 (predicted-was-too-pessimistic at N=32 across all scenarios).
SATURATION_BATCH = 24


def _active_param_bytes(model: ModelSpec, weight_q: str) -> float:
    """Bytes of model parameters streamed per decoded token.

    For dense models, this is the full parameter set in the chosen weight dtype.
    For MoE models, only the active experts are streamed per token, so this is
    correspondingly smaller (e.g. Mixtral 8x7B: 2/8 of FFN params per token).
    """
    bytes_per = WEIGHT_BYTES[weight_q]
    if model.is_moe and model.num_experts > 0 and model.active_experts > 0:
        # Conservative approximation: ~85% of params live in FFN/expert layers
        # for typical MoE models; the 15% non-expert (attention, embed, norms)
        # is always streamed. This avoids the trap of assuming the full active
        # ratio applies to every byte of the model.
        active_ratio = model.active_experts / model.num_experts
        non_expert_share = 0.15
        effective_ratio = non_expert_share + (1.0 - non_expert_share) * active_ratio
        return model.params * bytes_per * effective_ratio
    return model.params * bytes_per


def single_stream_decode_tok_s(
    inst:        Instance,
    model:       ModelSpec,
    weight_q:    str,
    total_gpus:  int,
    shared_mode: bool = False,
    kv_q:        str = "fp16",
    tp_degree:   int = 1,
    pp_degree:   int = 1,
) -> float:
    """Estimated single-user decode tok/s (no concurrency contention).

    With multi-GPU sharding (TP or PP), aggregate HBM bandwidth scales linearly
    with GPU count — TP shards each layer across GPUs (parallel reads), PP
    pipelines layer groups (sequential reads, but each stage reads less). For
    first-order purposes both produce roughly the same speedup vs. single-GPU.
    With time-slicing (shared_mode), 4 tenants share the same physical HBM, so
    bandwidth per tenant is divided by TIME_SLICE_REPLICAS.

    When the requested dtype lacks native tensor-core support on this GPU
    (e.g. fp8 on Ampere, int4 on Turing), vLLM falls back to slow software
    kernels — empirically ~50% throughput of the bandwidth-roofline estimate.
    Apply that penalty so SLO checks reflect real-world performance, not the
    paper-spec ceiling.

    Multi-GPU parallelism adds all-reduce or pipeline-bubble overhead that
    reduces effective throughput vs. raw bandwidth. The TP_OVERHEAD_*  /
    PP_OVERHEAD tables encode measured penalties (calibrated against TP=4
    NVLink: 0.45x measured for gemma-3-12b on A100).
    """
    if inst.hbm_bandwidth_tb_s <= 0:
        return 0.0
    active_bytes = _active_param_bytes(model, weight_q)
    if active_bytes <= 0:
        return 0.0
    # Effective HBM bandwidth for SINGLE-STREAM decode.
    # - TP aggregates bandwidth: each layer's matmul is split across `tp_degree`
    #   GPUs working in parallel (with all-reduce after).
    # - PP does NOT aggregate bandwidth for single-stream: stages run
    #   sequentially, so total time = sum-of-per-stage-times = roughly the
    #   same as a single GPU traversing all the model's weights. PP only
    #   helps at high concurrency where stages can overlap (pipelining).
    # Validated against gemma-3-12b on A100 NVLink (May 2026):
    #   TP=4: measured 87 tok/s — predicted 94 with TP-only aggregation
    #   PP=4: measured 35 tok/s — would predict 42 with no PP aggregation
    # The pre-fix v2 (which aggregated bandwidth across PP) over-predicted
    # PP throughput by 2.5×.
    effective_hbm_bs = inst.hbm_bandwidth_tb_s * 1e12 * tp_degree
    if shared_mode:
        effective_hbm_bs /= TIME_SLICE_REPLICAS
    # Pick the kernel-efficiency factor that matches the requested weight dtype.
    efficiency = DECODE_KERNEL_EFFICIENCY_BY_DTYPE.get(
        weight_q, DECODE_KERNEL_EFFICIENCY,
    )
    base = effective_hbm_bs / active_bytes * efficiency
    # Software-emulation penalty for unsupported dtypes.
    if _quant_compat_warning(inst, weight_q, kv_q) is not None:
        base *= 0.5
    # Multi-GPU parallelism overhead (all-reduce for TP, pipeline bubble for PP).
    if tp_degree > 1 or pp_degree > 1:
        base *= _parallelism_efficiency(tp_degree, pp_degree, inst.has_nvlink)
    return base


def per_user_tok_s(single_stream: float, concurrency: int) -> float:
    """Decay per-user decode tok/s with concurrency.

    Below SATURATION_BATCH, per-user throughput is **flat** — vLLM's continuous
    batching amortizes weight reads across the entire batch in the
    bandwidth-bound regime, so adding more sequences is essentially free for
    per-user throughput up to the saturation point. Verified empirically with
    AWQ INT4 gemma-3-27b on L40S: per-user tok/s held within ±3% from N=1 to
    N=24, then began to degrade.

    Above SATURATION_BATCH, decay is sub-linear at 1/sqrt(N/B_sat) — continuous
    batching still provides partial amortization even when compute-bound, so
    degradation is gentler than the naive 1/N model. The formula is continuous
    at the crossover (returns single_stream at N=B_sat from either side).
    """
    if concurrency <= 1 or single_stream <= 0:
        return single_stream
    if concurrency <= SATURATION_BATCH:
        return single_stream                           # truly flat below sat
    return single_stream / math.sqrt(concurrency / SATURATION_BATCH)
