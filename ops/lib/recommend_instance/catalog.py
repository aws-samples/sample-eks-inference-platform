"""Instance catalog, VRAM/KV byte tables, cluster constants, and quant-vs-hardware
compatibility checks."""

from __future__ import annotations

from dataclasses import dataclass


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

HF_API = "https://huggingface.co/api/models/{}"
HF_RAW_CONFIG = "https://huggingface.co/{}/resolve/main/config.json"

# Bytes per element for weights, by quantisation label.
WEIGHT_BYTES = {
    "fp32": 4.0,
    "fp16": 2.0,
    "bf16": 2.0,
    "int8": 1.0,
    "fp8":  1.0,
    "int4": 0.5,
    "gptq4": 0.5,
    "awq4":  0.5,
    "nf4":   0.5,
}

# Bytes per element for KV cache.
KV_BYTES = {
    "fp16": 2.0,
    "bf16": 2.0,
    "fp8":  1.0,
    "int8": 1.0,
}

# Cluster Karpenter NodePool constraints (from terraform/30.eks/30.cluster/karpenter/).
# Both NodePools now accept any NVIDIA-backed G or P instance. `gpu-shared`
# additionally requires a single GPU per node (time-slicing precondition).
CLUSTER_INFERENCE_CATEGORIES = {"g", "p"}
CLUSTER_SHARED_CATEGORIES    = {"g"}
CLUSTER_GPU_MANUFACTURER     = "nvidia"
# Matches the `replicas: 4` in terraform/30.eks/30.cluster/karpenter/gpu-shared.yaml
# under kubelet-device-plugins.nvidia.time-slicing. When gpu-shared is used,
# NVIDIA advertises 1 physical GPU as this many logical slots. Time-slicing
# does NOT partition GPU memory — all co-located models compete for the full
# VRAM, so a model is shared-eligible only if TIME_SLICE_REPLICAS copies of it
# fit in the GPU's memory.
TIME_SLICE_REPLICAS = 4
# Fallback pricing region — matches the static catalog. Overridden when the
# Pricing API is reachable or a cached snapshot exists for the target region.
CATALOG_PRICING_REGION = "us-east-1"
PRICING_CACHE_TTL_SEC  = 30 * 24 * 3600  # 30 days

# Valid GPU counts for vLLM tensor parallelism.
TP_DEGREES = (1, 2, 4, 8)


# --------------------------------------------------------------------------- #
# Instance catalog                                                            #
#                                                                             #
# Prices are approximate us-east-1 on-demand ($/hr) and are used only when the #
# AWS Pricing API cannot be reached. With boto3 available, per-region prices   #
# are fetched at runtime and cached to ~/.cache/ai-platform/pricing-*.json.    #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Instance:
    name:                str
    gpu:                 str
    num_gpus:            int
    vram_gb:             int     # per GPU
    vcpu:                int
    mem_gb:              int     # host RAM
    price_usd_h:         float   # us-east-1 on-demand, approximate fallback
    gpu_manufacturer:    str   = "nvidia"
    has_nvlink:          bool  = False  # NVLink interconnect (affects TP vs PP choice)
    hbm_bandwidth_tb_s:  float = 0.0    # per-GPU HBM bandwidth (TB/s) — drives decode tok/s
    compute_capability:  str   = ""     # NVIDIA CUDA compute capability ("7.5", "8.0", ...)
    arch_family:         str   = ""     # "Turing" | "Ampere" | "Ada" | "Hopper" | "Blackwell"

    @property
    def family(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def size(self) -> str:
        return self.name.split(".", 1)[1]

    @property
    def category(self) -> str:
        """G or P (first letter of the family)."""
        return self.family[0]

    @property
    def total_vram_gb(self) -> int:
        return self.vram_gb * self.num_gpus

    @property
    def total_hbm_tb_s(self) -> float:
        """Aggregate HBM bandwidth across all GPUs on the instance."""
        return self.hbm_bandwidth_tb_s * self.num_gpus


# HBM bandwidth values are per-GPU, taken from NVIDIA datasheets:
#   T4   320 GB/s   (Turing,  cc 7.5,  fp16 tensor cores only — no native bf16/fp8/int4-Marlin)
#   L4   300 GB/s   (Ada,     cc 8.9,  bf16 + fp8 + int4 OK)
#   A10G 600 GB/s   (Ampere,  cc 8.6,  bf16 + int4 OK; no fp8)
#   L40S 864 GB/s   (Ada,     cc 8.9,  bf16 + fp8 + int4 OK)
#   A100-40 1555 GB/s (Ampere, cc 8.0, bf16 + int4 OK; no fp8)
#   A100-80 2039 GB/s (Ampere, cc 8.0, bf16 + int4 OK; no fp8)
#   H100  3350 GB/s (Hopper,  cc 9.0,  fp8 + bf16 + int4 OK)
#   H200  4800 GB/s (Hopper,  cc 9.0,  fp8 + bf16 + int4 OK)
INSTANCES: list[Instance] = [
    # g4dn — T4 (no NVLink, Turing — limited dtype support)
    Instance("g4dn.xlarge",   "T4",         1, 16,   4,  16,  0.526,
             hbm_bandwidth_tb_s=0.32, compute_capability="7.5", arch_family="Turing"),
    Instance("g4dn.2xlarge",  "T4",         1, 16,   8,  32,  0.752,
             hbm_bandwidth_tb_s=0.32, compute_capability="7.5", arch_family="Turing"),
    Instance("g4dn.12xlarge", "T4",         4, 16,  48, 192,  3.912,
             hbm_bandwidth_tb_s=0.32, compute_capability="7.5", arch_family="Turing"),

    # g5 — A10G (no NVLink, Ampere)
    Instance("g5.xlarge",     "A10G",       1, 24,   4,  16,  1.006,
             hbm_bandwidth_tb_s=0.60, compute_capability="8.6", arch_family="Ampere"),
    Instance("g5.2xlarge",    "A10G",       1, 24,   8,  32,  1.212,
             hbm_bandwidth_tb_s=0.60, compute_capability="8.6", arch_family="Ampere"),
    Instance("g5.4xlarge",    "A10G",       1, 24,  16,  64,  1.624,
             hbm_bandwidth_tb_s=0.60, compute_capability="8.6", arch_family="Ampere"),
    Instance("g5.12xlarge",   "A10G",       4, 24,  48, 192,  5.672,
             hbm_bandwidth_tb_s=0.60, compute_capability="8.6", arch_family="Ampere"),
    Instance("g5.48xlarge",   "A10G",       8, 24, 192, 768, 16.288,
             hbm_bandwidth_tb_s=0.60, compute_capability="8.6", arch_family="Ampere"),

    # g6 — L4 (no NVLink, Ada — supports fp8)
    Instance("g6.xlarge",     "L4",         1, 24,   4,  16,  0.805,
             hbm_bandwidth_tb_s=0.30, compute_capability="8.9", arch_family="Ada"),
    Instance("g6.2xlarge",    "L4",         1, 24,   8,  32,  0.978,
             hbm_bandwidth_tb_s=0.30, compute_capability="8.9", arch_family="Ada"),
    Instance("g6.4xlarge",    "L4",         1, 24,  16,  64,  1.323,
             hbm_bandwidth_tb_s=0.30, compute_capability="8.9", arch_family="Ada"),
    Instance("g6.12xlarge",   "L4",         4, 24,  48, 192,  4.602,
             hbm_bandwidth_tb_s=0.30, compute_capability="8.9", arch_family="Ada"),
    Instance("g6.48xlarge",   "L4",         8, 24, 192, 768, 13.350,
             hbm_bandwidth_tb_s=0.30, compute_capability="8.9", arch_family="Ada"),

    # g6e — L40S (no NVLink, Ada — supports fp8; prefer PP over TP for multi-GPU)
    Instance("g6e.xlarge",    "L40S",       1, 48,   4,  32,  1.861,
             hbm_bandwidth_tb_s=0.86, compute_capability="8.9", arch_family="Ada"),
    Instance("g6e.2xlarge",   "L40S",       1, 48,   8,  64,  2.242,
             hbm_bandwidth_tb_s=0.86, compute_capability="8.9", arch_family="Ada"),
    Instance("g6e.4xlarge",   "L40S",       1, 48,  16, 128,  3.004,
             hbm_bandwidth_tb_s=0.86, compute_capability="8.9", arch_family="Ada"),
    Instance("g6e.12xlarge",  "L40S",       4, 48,  48, 384, 10.493,
             hbm_bandwidth_tb_s=0.86, compute_capability="8.9", arch_family="Ada"),
    Instance("g6e.48xlarge",  "L40S",       8, 48, 192, 1536, 30.131,
             hbm_bandwidth_tb_s=0.86, compute_capability="8.9", arch_family="Ada"),

    # p4d / p4de — A100 (NVLink, Ampere)
    Instance("p4d.24xlarge",  "A100 40GB",  8, 40,  96, 1152, 32.773, has_nvlink=True,
             hbm_bandwidth_tb_s=1.555, compute_capability="8.0", arch_family="Ampere"),
    Instance("p4de.24xlarge", "A100 80GB",  8, 80,  96, 1152, 40.966, has_nvlink=True,
             hbm_bandwidth_tb_s=2.039, compute_capability="8.0", arch_family="Ampere"),

    # p5 / p5e / p5en — H100 / H200 (NVLink, Hopper — supports fp8)
    Instance("p5.48xlarge",   "H100 80GB",  8, 80, 192, 2048, 98.320, has_nvlink=True,
             hbm_bandwidth_tb_s=3.35, compute_capability="9.0", arch_family="Hopper"),
    Instance("p5e.48xlarge",  "H200 141GB", 8, 141, 192, 2048, 118.020, has_nvlink=True,
             hbm_bandwidth_tb_s=4.80, compute_capability="9.0", arch_family="Hopper"),
    Instance("p5en.48xlarge", "H200 141GB", 8, 141, 192, 2048, 124.000, has_nvlink=True,
             hbm_bandwidth_tb_s=4.80, compute_capability="9.0", arch_family="Hopper"),
]


# --------------------------------------------------------------------------- #
# Quant ↔ Hardware compatibility (PR 4)                                       #
# --------------------------------------------------------------------------- #

# Min compute capability required for native tensor-core support of each dtype.
# Lower compute capability falls back to slow software emulation, often defeating
# the purpose of the optimization.
QUANT_MIN_COMPUTE_CAPABILITY = {
    "fp32":  "7.5",   # universal
    "fp16":  "7.0",   # Volta+
    "bf16":  "8.0",   # Ampere+ (T4/Turing has no native bf16 tensor cores)
    "int8":  "7.5",   # Turing+ via DP4A
    "fp8":   "8.9",   # Ada+ (L4, L40S, H100, H200, B200)
    "int4":  "8.0",   # Ampere+ for Marlin/AWQ/GPTQ kernels
    "gptq4": "8.0",   # same as int4
    "awq4":  "8.0",   # same as int4
    "nf4":   "8.0",   # bitsandbytes 4-bit
}

# Same table for KV cache dtypes.
KV_QUANT_MIN_COMPUTE_CAPABILITY = {
    "fp16":  "7.0",
    "bf16":  "8.0",
    "fp8":   "8.9",
    "int8":  "7.5",
}


def _cc_lt(a: str, b: str) -> bool:
    """Compare two compute capability strings (e.g. '8.0' < '8.9')."""
    if not a or not b:
        return False
    try:
        return tuple(int(x) for x in a.split(".")) < tuple(int(x) for x in b.split("."))
    except ValueError:
        return False


def _quant_compat_warning(inst: Instance, weight_q: str, kv_q: str) -> str | None:
    """Return a human-readable warning if the requested quants don't match the GPU.

    Returns None when the combination is fully supported.
    """
    issues: list[str] = []

    min_cc_w = QUANT_MIN_COMPUTE_CAPABILITY.get(weight_q)
    if min_cc_w and inst.compute_capability and _cc_lt(inst.compute_capability, min_cc_w):
        issues.append(
            f"weights {weight_q} needs cc≥{min_cc_w}, "
            f"{inst.gpu} is cc{inst.compute_capability} ({inst.arch_family}) — "
            f"falls back to software emulation"
        )

    min_cc_k = KV_QUANT_MIN_COMPUTE_CAPABILITY.get(kv_q)
    if min_cc_k and inst.compute_capability and _cc_lt(inst.compute_capability, min_cc_k):
        issues.append(
            f"KV cache {kv_q} needs cc≥{min_cc_k}, "
            f"{inst.gpu} is cc{inst.compute_capability}"
        )

    return "; ".join(issues) if issues else None
