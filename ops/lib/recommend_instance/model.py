"""Model metadata: resolve architecture and parameter count from HuggingFace."""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .catalog import HF_API, HF_RAW_CONFIG


def _build_ssl_context() -> ssl.SSLContext:
    """Return an SSL context that works across common macOS Python installs.

    python.org CPython on macOS ships without a linked system CA store; fall
    back to certifi if installed (standard on most dev machines) before giving
    up.
    """
    ctx = ssl.create_default_context()
    if ctx.get_ca_certs():
        return ctx
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ctx


_SSL_CONTEXT = _build_ssl_context()


# --------------------------------------------------------------------------- #
# Model metadata                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class ModelSpec:
    model_id:       str
    params:         int            # total parameters (all experts for MoE)
    num_layers:     int
    hidden_size:    int
    num_heads:      int
    num_kv_heads:   int            # == num_heads for MHA, smaller for GQA/MQA
    head_dim:       int
    vocab_size:     int
    max_position:   int
    architecture:   str = ""
    is_moe:         bool = False
    num_experts:    int = 0
    active_experts: int = 0
    warnings:       list[str] = field(default_factory=list)


def _http_get_json(url: str, token: str | None) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "recommend-instance/1.0"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode())


def fetch_model(model_id: str, token: str | None) -> ModelSpec:
    """Resolve model architecture + parameter count from HuggingFace."""
    print(f"  Fetching {model_id} from HuggingFace...", end="", flush=True,
          file=sys.stderr)
    warnings: list[str] = []

    # 1. config.json — canonical source of architecture.
    try:
        cfg = _http_get_json(HF_RAW_CONFIG.format(model_id), token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit(f"error: {model_id} is gated. Set HF_TOKEN=hf_... and retry.")
        if e.code == 404:
            # Distinguish "model doesn't exist" from "model exists but has no
            # config.json" (typical for GGUF-only repos, ONNX exports, etc).
            try:
                _http_get_json(HF_API.format(model_id), token)
                sys.exit(
                    f"error: {model_id} exists on HuggingFace but has no config.json "
                    f"(typical for GGUF/ONNX/quant-only repos). The recommender needs "
                    f"the original transformer config — point to the source HF model "
                    f"instead of the converted artifact."
                )
            except Exception:
                sys.exit(f"error: model {model_id!r} not found on HuggingFace.")
        sys.exit(f"error: failed to fetch config.json for {model_id}: {e}")
    except urllib.error.URLError as e:
        sys.exit(f"error: network failure fetching {model_id}: {e.reason}")

    # Some multi-modal models nest the text config.
    if "text_config" in cfg and isinstance(cfg["text_config"], dict):
        warnings.append("multi-modal model; using text_config for sizing")
        cfg = {**cfg, **cfg["text_config"]}

    try:
        num_layers   = int(cfg.get("num_hidden_layers") or cfg["n_layer"])
        hidden_size  = int(cfg.get("hidden_size")       or cfg["n_embd"])
        num_heads    = int(cfg.get("num_attention_heads") or cfg["n_head"])
        vocab_size   = int(cfg.get("vocab_size", 32000))
    except (KeyError, TypeError, ValueError):
        sys.exit(f"error: {model_id} config.json missing required transformer fields.")

    num_kv_heads = int(cfg.get("num_key_value_heads", num_heads))
    head_dim     = int(cfg.get("head_dim", hidden_size // num_heads))
    max_position = int(cfg.get("max_position_embeddings", 4096))
    intermediate = int(cfg.get("intermediate_size", 4 * hidden_size))
    architecture = (cfg.get("architectures") or [""])[0]

    # MoE detection.
    num_experts    = int(cfg.get("num_local_experts") or cfg.get("num_experts") or 0)
    active_experts = int(cfg.get("num_experts_per_tok") or cfg.get("moe_k") or 0)
    is_moe         = num_experts > 1

    # 2. Parameter count — try HF's reported safetensors total first.
    params: int | None = None
    try:
        info = _http_get_json(HF_API.format(model_id), token)
        st = info.get("safetensors") or {}
        # Prefer the per-dtype parameter sum when present — it's typically
        # more reliable than `total`. Some models (e.g. Qwen/WebWorld-32B
        # in May 2026) have a wonky `total` field reporting tensor-count
        # rather than parameter-count, so we'd see 676K instead of 32B.
        per_dtype = st.get("parameters", {})
        if isinstance(per_dtype, dict) and per_dtype:
            params_sum = sum(v for v in per_dtype.values() if isinstance(v, int))
            if params_sum > 1_000_000:    # sanity threshold: must be > 1M params
                params = params_sum
        # Fall back to total if parameters block unusable.
        if params is None and "total" in st and isinstance(st["total"], int):
            if st["total"] > 1_000_000:
                params = st["total"]
            else:
                # 'total' is suspiciously small — likely tensor count, not param count.
                warnings.append(
                    f"safetensors.total={st['total']:,} looks wrong for a real LLM; "
                    f"falling back to architectural estimate"
                )
    except Exception:
        pass

    # 3. Fall back to architectural estimate.
    if not params:
        warnings.append("safetensors param count unavailable; using architectural estimate")
        embed = vocab_size * hidden_size
        # Attention: Q (hidden * hidden) + K,V (hidden * num_kv_heads*head_dim) + O (hidden * hidden)
        attn = (2 * hidden_size * hidden_size) + (2 * hidden_size * num_kv_heads * head_dim)
        if is_moe and num_experts > 0:
            # All experts stored in VRAM, all counted in total params.
            ffn = num_experts * (3 * hidden_size * intermediate)
        else:
            ffn = 3 * hidden_size * intermediate  # gated FFN (gate + up + down)
        per_layer = attn + ffn + (2 * hidden_size)  # + layernorms (tiny)
        params = embed + num_layers * per_layer + hidden_size  # final norm

    print(" done.", file=sys.stderr)
    return ModelSpec(
        model_id=model_id,
        params=params,
        num_layers=num_layers,
        hidden_size=hidden_size,
        num_heads=num_heads,
        num_kv_heads=num_kv_heads,
        head_dim=head_dim,
        vocab_size=vocab_size,
        max_position=max_position,
        architecture=architecture,
        is_moe=is_moe,
        num_experts=num_experts,
        active_experts=active_experts,
        warnings=warnings,
    )
