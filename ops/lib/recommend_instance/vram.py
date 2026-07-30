"""Per-GPU VRAM estimation for serving a model."""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import KV_BYTES, WEIGHT_BYTES
from .model import ModelSpec


# --------------------------------------------------------------------------- #
# VRAM estimation                                                             #
# --------------------------------------------------------------------------- #

@dataclass
class VramEstimate:
    weights_gb:     float
    kv_cache_gb:    float
    activations_gb: float
    overhead_gb:    float
    # Per-sequence KV at the model's MAX seq_len — used for the headline
    # "does it fit?" VRAM check. Sized for the worst case (every user fills the
    # context cap to the brim) so we don't OOM under peak.
    kv_per_seq_gb:  float = 0.0
    # Per-sequence KV at the EXPECTED working seq_len (avg input+output tokens).
    # Used for concurrency budget calculations. With vLLM's PagedAttention, KV
    # is allocated per-token-in-flight rather than reserved for the full
    # max_model_len — so realistic concurrency is much higher than worst-case
    # math suggests.
    kv_per_active_seq_gb: float = 0.0

    @property
    def total_gb(self) -> float:
        return self.weights_gb + self.kv_cache_gb + self.activations_gb + self.overhead_gb


def estimate_vram(
    m:                 ModelSpec,
    weight_q:          str,
    kv_q:              str,
    seq_len:           int,
    batch_size:        int,
    users:             int,
    avg_context_len:   int | None = None,
) -> VramEstimate:
    """
    Per-GPU VRAM required to serve `m`, ignoring tensor parallelism (TP=1).
    Dividing this total by the TP degree gives a first-order per-GPU estimate.

    `seq_len` is the model's max context window (max_model_len). It's used for
    the worst-case "does it fit?" VRAM check.

    `avg_context_len` is the typical input+output tokens per active sequence
    under real workload (PagedAttention only allocates KV for tokens actually
    in flight, not for the full max_model_len). It's used for concurrency
    budget calculations. Defaults to seq_len // 4 — a reasonable mid-range for
    chat workloads. Set equal to seq_len for worst-case sizing.
    """
    w_bytes = WEIGHT_BYTES[weight_q]
    k_bytes = KV_BYTES[kv_q]

    weights = m.params * w_bytes

    # KV cache per token = 2 (K+V) × layers × num_kv_heads × head_dim × bytes.
    # Effective concurrency in vLLM ≈ max(batch_size, users).
    concurrency = max(batch_size, users)
    kv_per_tok  = 2 * m.num_layers * m.num_kv_heads * m.head_dim * k_bytes

    # Worst-case KV cache (every active seq fills max_model_len) — used for
    # the headline "does it fit?" VRAM check.
    kv_cache    = kv_per_tok * seq_len * concurrency

    # Activations: rough upper bound for prefill — O(batch * seq * hidden).
    # vLLM / Flash-Attention reduces this materially; factor 4 is a safe pad.
    activations = 4 * batch_size * seq_len * m.hidden_size * 2  # fp16 activations

    # KV cache for a single sequence at MAX seq_len (used by the worst-case
    # headline VRAM check via kv_cache above).
    kv_per_seq = kv_per_tok * seq_len

    # Per-active-sequence KV at the EXPECTED working context length. With
    # PagedAttention, this is what concurrency calculations should divide into
    # the available KV budget. Default to seq_len // 4 if not specified.
    if avg_context_len is None:
        # Default to 1024 (chat-grade workload) capped at seq_len. Was previously
        # seq_len // 4, which over-estimated KV usage for default-mode users
        # (they typically run short prompts/short outputs, not max_model_len-sized
        # working sets). For long-context workloads, set --avg-context explicitly
        # or use --workload summarization.
        avg_context_len = min(1024, seq_len)
    avg_context_len = max(1, min(avg_context_len, seq_len))
    kv_per_active_seq = kv_per_tok * avg_context_len

    # 15% pad for framework, CUDA graphs, allocator fragmentation.
    subtotal = weights + kv_cache + activations
    overhead = 0.15 * subtotal

    gb = 1024 ** 3
    return VramEstimate(
        weights_gb=weights / gb,
        kv_cache_gb=kv_cache / gb,
        activations_gb=activations / gb,
        overhead_gb=overhead / gb,
        kv_per_seq_gb=kv_per_seq / gb,
        kv_per_active_seq_gb=kv_per_active_seq / gb,
    )
