"""Option model and the core instance recommender (VRAM fit + parallelism + price)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .catalog import (
    CLUSTER_GPU_MANUFACTURER,
    CLUSTER_INFERENCE_CATEGORIES,
    CLUSTER_SHARED_CATEGORIES,
    INSTANCES,
    TIME_SLICE_REPLICAS,
    TP_DEGREES,
    WEIGHT_BYTES,
    Instance,
    _quant_compat_warning,
)
from .model import ModelSpec
from .throughput import per_user_tok_s, single_stream_decode_tok_s


# --------------------------------------------------------------------------- #
# Recommendation                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class Option:
    instance:           Instance
    tp_degree:          int
    pp_degree:          int      # pipeline-parallel stages (1 = no PP)
    per_gpu_need_gb:    float
    headroom_gb:        float
    price_usd_h:        float    # effective price (from Pricing API or fallback)
    nodepool:           str      # "gpu-inference" | "gpu-shared" | "out-of-cluster"
    shared_eligible:    bool     # can legitimately use gpu-shared time-slicing
    over_price_ceiling: bool
    # Throughput estimates (PR 2): single-stream is ceiling, per-user reflects
    # the user's --users concurrency setting.
    single_stream_tok_s: float = 0.0
    per_user_tok_s_at_users: float = 0.0
    # Quant compatibility (PR 4): None when fully supported, str warning otherwise.
    quant_warning:      str | None = None
    notes:              list[str] = field(default_factory=list)

    @property
    def total_gpus(self) -> int:
        return self.tp_degree * self.pp_degree

    @property
    def parallelism_label(self) -> str:
        if self.tp_degree > 1 and self.pp_degree > 1:
            return f"TP={self.tp_degree} × PP={self.pp_degree}"
        if self.pp_degree > 1:
            return f"PP={self.pp_degree}"
        if self.tp_degree > 1:
            return f"TP={self.tp_degree}"
        return ""

    @property
    def in_cluster(self) -> bool:
        return self.nodepool != "out-of-cluster"

    @property
    def effective_price_usd_h(self) -> float:
        return self.price_usd_h

    @property
    def amortised_price_usd_h(self) -> float | None:
        if self.shared_eligible and self.total_gpus == 1:
            return self.price_usd_h / TIME_SLICE_REPLICAS
        return None

    @property
    def sort_key(self) -> tuple:
        # Prefer: quant-compatible, below ceiling, in-cluster, lowest price, fewest GPUs.
        return (
            self.quant_warning is not None,
            self.over_price_ceiling,
            not self.in_cluster,
            self.effective_price_usd_h,
            self.total_gpus,
        )


def _tp_overhead(tp: int) -> float:
    """Multiplicative pad for tensor-parallel activation duplication and comm buffers."""
    return 1.0 if tp == 1 else 1.0 + 0.05 * math.log2(tp)


def _classify_nodepool(
    inst:              Instance,
    tp:                int,
    per_gpu_need_gb:   float,
    safety_margin:     float,
) -> tuple[str, bool]:
    """Which NodePool can schedule this instance, and is it shared-eligible?

    Returns (nodepool, shared_eligible). Mirrors the requirements in
    terraform/30.eks/30.cluster/karpenter/gpu-{inference,shared}.yaml.

    Shared eligibility requires that TIME_SLICE_REPLICAS copies of the model
    fit within the GPU's memory budget, because NVIDIA time-slicing shares
    compute but NOT memory — all co-tenants compete for the same VRAM.
    """
    if inst.gpu_manufacturer != CLUSTER_GPU_MANUFACTURER:
        return "out-of-cluster", False

    # gpu-shared: single-GPU G-family NVIDIA where TIME_SLICE_REPLICAS copies fit.
    available_per_slice = inst.vram_gb * (1.0 - safety_margin) / TIME_SLICE_REPLICAS
    shared_eligible = (
        inst.category in CLUSTER_SHARED_CATEGORIES
        and inst.num_gpus == 1
        and per_gpu_need_gb <= available_per_slice
    )

    # gpu-inference: any NVIDIA G or P instance.
    if inst.category in CLUSTER_INFERENCE_CATEGORIES:
        # When TP == num_gpus and > 1, we need the whole dedicated node;
        # gpu-inference serves both single- and multi-GPU cases.
        return "gpu-inference", shared_eligible

    return "out-of-cluster", False


def _valid_parallelism_configs(
    inst: Instance,
    num_heads: int,
    tp_pin: int | None,
) -> list[tuple[int, int]]:
    """Generate valid (tp, pp) pairs for an instance, respecting vLLM constraints.

    Rules from https://docs.vllm.ai/en/latest/serving/parallelism_scaling/:
    - TP: num_attention_heads must be evenly divisible by tp_degree
    - PP: splits model along layer boundaries (no head constraint)
    - TP × PP must equal the instance's GPU count

    Returns ALL valid configurations unsorted. Selection between configs is
    done by `_best_config_for_instance` based on predicted throughput at the
    user's target concurrency — not by a hardcoded interconnect heuristic.
    Earlier versions of this function preferred TP on NVLink and PP on PCIe;
    that was a rule of thumb. With the calibrated throughput model we can
    just compute and pick.
    """
    n = inst.num_gpus
    if n == 1:
        if tp_pin is not None and tp_pin != 1:
            return []
        return [(1, 1)]

    configs: list[tuple[int, int]] = []

    # Enumerate all factorizations of num_gpus into tp × pp
    for tp in TP_DEGREES:
        if tp > n:
            break
        if n % tp != 0:
            continue
        pp = n // tp
        # TP constraint: attention heads must be divisible by tp
        if tp > 1 and num_heads % tp != 0:
            continue
        # If user pinned TP, only allow that exact value
        if tp_pin is not None and tp != tp_pin:
            continue
        configs.append((tp, pp))

    return configs


def _best_config_for_instance(
    inst:       Instance,
    model:      ModelSpec,
    weight_q:   str,
    kv_q:       str,
    configs:    list[tuple[int, int]],
    users:      int,
) -> tuple[int, int] | None:
    """Pick the (tp, pp) config that maximizes per-user throughput at the
    target concurrency.

    This makes v2 a true throughput optimizer — it now decides between TP,
    PP, and TP×PP based on the calibrated throughput model rather than a
    hardcoded interconnect rule. For NVLink instances this typically picks
    max-TP (the old heuristic was right). For PCIe instances the choice
    depends on the model size and concurrency target — sometimes TP=4 PCIe
    with its 0.25× efficiency factor still beats PP=4 because PP doesn't
    aggregate bandwidth at all for single-stream.

    Tiebreak: prefer the lower TP × PP product (less coordination overhead),
    then prefer max TP (NVLink-style) when ties remain. None ⇒ no valid
    configs.
    """
    if not configs:
        return None
    if len(configs) == 1:
        return configs[0]

    scored: list[tuple[float, int, int]] = []
    for tp, pp in configs:
        ss = single_stream_decode_tok_s(
            inst, model, weight_q, tp * pp,
            shared_mode=False, kv_q=kv_q,
            tp_degree=tp, pp_degree=pp,
        )
        score = per_user_tok_s(ss, max(1, users))
        # Tiebreaker: smaller (tp+pp) wins (less coordination), then larger TP wins.
        scored.append((score, -(tp + pp), tp))
    scored.sort(reverse=True)
    best_idx = scored[0]
    # Find back the actual (tp, pp) tuple corresponding to the winner.
    best_tp = best_idx[2]
    for tp, pp in configs:
        if tp == best_tp and per_user_tok_s(
            single_stream_decode_tok_s(
                inst, model, weight_q, tp * pp,
                shared_mode=False, kv_q=kv_q,
                tp_degree=tp, pp_degree=pp,
            ),
            max(1, users),
        ) == best_idx[0]:
            return (tp, pp)
    return configs[0]


def find_options(
    total_need_gb:      float,
    num_heads:          int,
    tp_pin:             int | None,
    require_in_cluster: bool,
    safety_margin:      float,
    prices:             dict[str, float],
    max_price:          float,
    # Extra context needed for throughput + quant-compat (added in PR 2 / PR 4):
    model:              ModelSpec | None = None,
    weight_q:           str = "bf16",
    kv_q:               str = "fp16",
    users:              int = 1,
) -> list[Option]:
    options: list[Option] = []

    for inst in INSTANCES:
        configs = _valid_parallelism_configs(inst, num_heads, tp_pin)
        if not configs:
            continue

        # Pick the (tp, pp) config that maximizes per-user throughput at the
        # target concurrency. This is what makes v2 a true optimizer rather
        # than a heuristic-driven recommender. When `model` is None (no
        # context), fall back to the natural TP=gpuCount choice.
        if model is not None:
            selected = _best_config_for_instance(
                inst, model, weight_q, kv_q, configs, users,
            )
            if selected is None:
                continue
            tp, pp = selected
        else:
            tp, pp = configs[0]
        total_gpus = tp * pp

        # Per-GPU VRAM: weights sharded across all GPUs (TP shards layers,
        # PP shards layer groups). KV cache and activations are per-stage.
        per_gpu = (total_need_gb / total_gpus) * _tp_overhead(tp)
        available = inst.vram_gb * (1.0 - safety_margin)
        if per_gpu > available:
            continue

        # Host RAM check — the worker pod requests workerMemory ≈ weights+4 GiB
        # of host RAM (vLLM stages weights through CPU during init). The
        # instance must have enough host RAM after the K8s system reservation
        # and daemonsets (~4 GiB). Use a 4 GiB safety buffer above the
        # workerMemory request.
        if model is not None:
            weights_gb_for_check = (
                model.params * WEIGHT_BYTES[weight_q] / (1024 ** 3)
            )
            required_worker_mem_gib = max(8, math.ceil(weights_gb_for_check + 4))
            host_mem_required_gib = required_worker_mem_gib + 4   # K8s + system overhead
            if inst.mem_gb < host_mem_required_gib:
                continue   # instance can't host the worker pod even before scheduling overhead

        nodepool, shared_eligible = _classify_nodepool(
            inst, tp, per_gpu, safety_margin,
        )
        if require_in_cluster and nodepool == "out-of-cluster":
            continue

        price = prices.get(inst.name, inst.price_usd_h)
        over_ceiling = price > max_price

        notes: list[str] = []
        if tp > 1 and pp > 1:
            notes.append(f"TP={tp} × PP={pp} across {total_gpus} GPUs")
        elif pp > 1:
            notes.append(f"pipeline-parallel across {pp} GPUs")
        elif tp > 1:
            notes.append(f"tensor-parallel across {tp} GPUs"
                         + (" (NVLink)" if inst.has_nvlink else " (PCIe)"))
        if nodepool == "out-of-cluster":
            notes.append("not covered by Karpenter NodePools")
        if over_ceiling:
            notes.append(f"${price:.2f}/hr exceeds --max-price ${max_price:.2f}")

        # PR 2: throughput estimate. Always compute DEDICATED throughput —
        # fleet scaling and SLO checks need the full-bandwidth figure.
        # Shared-mode throughput (÷ TIME_SLICE_REPLICAS) is only relevant for
        # display when the user deploys a single model with `shared: true`;
        # that's handled in the presentation layer, not here.
        single_stream = 0.0
        per_user_at_users = 0.0
        if model is not None:
            single_stream = single_stream_decode_tok_s(
                inst, model, weight_q, total_gpus,
                shared_mode=False, kv_q=kv_q,
                tp_degree=tp, pp_degree=pp,
            )
            per_user_at_users = per_user_tok_s(single_stream, max(1, users))

        # PR 4: quant ↔ hardware compatibility check.
        quant_warn = _quant_compat_warning(inst, weight_q, kv_q)
        if quant_warn:
            notes.append(f"⚠ {quant_warn}")

        options.append(Option(
            instance=inst,
            tp_degree=tp,
            pp_degree=pp,
            per_gpu_need_gb=per_gpu,
            headroom_gb=inst.vram_gb - per_gpu,
            price_usd_h=price,
            nodepool=nodepool,
            shared_eligible=shared_eligible,
            over_price_ceiling=over_ceiling,
            single_stream_tok_s=single_stream,
            per_user_tok_s_at_users=per_user_at_users,
            quant_warning=quant_warn,
            notes=notes,
        ))

    options.sort(key=lambda o: o.sort_key)
    return options
