"""recommend-instance.py — Right-size EC2 GPU instances for an LLM on this EKS platform.

Reads model architecture from HuggingFace, estimates VRAM requirements, and
recommends the cheapest instance that fits — with the right parallelism strategy
(TP, PP, or TP×PP) based on vLLM best practices and GPU interconnect topology.
Adds a bandwidth-aware throughput model on top of VRAM sizing:
  - HBM bandwidth in the instance catalog → decode tok/s estimation
  - Per-instance single-stream + concurrent tok/s/user estimates
  - --target-tok-s SLO validation in fleet mode (escalates to faster GPUs if needed)
  - Quant ↔ hardware compatibility warnings (e.g. fp8 needs Ada/Hopper)
  - max_num_seqs emitted in the generated YAML
  - MoE bandwidth annotation (active vs total params)

Supports two modes (auto-selected based on whether one instance can handle --users):
  1. Single-instance sizing: pick the best GPU for one replica
  2. Fleet scaling: recommend instance type + replica count when one instance
     can't serve all users — validated against the per-user throughput SLO
     (--target-tok-s) when set

Parallelism strategy (per https://docs.vllm.ai/en/latest/serving/parallelism_scaling/):
  - Tensor Parallelism (TP): shards each layer's weights across GPUs (prefers NVLink)
  - Pipeline Parallelism (PP): assigns layer groups to pipeline stages (any interconnect)
  - TP×PP: combines both for large multi-GPU deployments
  - TP requires num_attention_heads to be evenly divisible by tp_degree

Throughput model (per https://blog.vllm.ai/2025/09/05/anatomy-of-vllm.html):
  - Decode is memory-bandwidth-bound: tok/s ≈ HBM_bandwidth / params_streamed_per_token
  - For MoE, only active experts are streamed per token (effective bandwidth is higher)
  - Per-user throughput decays with concurrency along a ~1/sqrt(N) curve up to
    saturation, then ~1/N. Estimates here are first-order (±25%); validate by
    running `vllm bench serve` against a real deployment.

Examples:

  # Quick check — what GPU fits a 4B model?
  ./ops/recommend-instance.py google/gemma-3-4b-it

  # 8B model with 16K context, 8 concurrent users per instance
  ./ops/recommend-instance.py meta-llama/Llama-3.1-8B-Instruct --seq 16384 --users 8

  # Quantised 32B model — int4 cuts VRAM in half
  ./ops/recommend-instance.py Qwen/Qwen2.5-32B-Instruct --quant int4

  # 50 concurrent users — auto-scales to fleet if one instance can't handle it
  ./ops/recommend-instance.py meta-llama/Llama-3.1-8B-Instruct --users 50

  # 100 users with latency SLO — forces faster GPUs and more replicas
  ./ops/recommend-instance.py Qwen/Qwen2.5-7B-Instruct --users 100 --target-tok-s 25

  # Pin to TP=4, only show in-cluster options, machine-readable output
  ./ops/recommend-instance.py meta-llama/Llama-3.1-70B-Instruct --tp 4 --in-cluster-only --json

  # Gated model (needs HuggingFace token)
  HF_TOKEN=hf_... ./ops/recommend-instance.py google/gemma-3-27b-it

  # Budget-conscious: cap at $5/hr per instance
  ./ops/recommend-instance.py Qwen/Qwen2.5-32B-Instruct --quant int4 --max-price 5

  # Recommend AND deploy: write the YAML, commit, and push (ArgoCD picks it up)
  ./ops/recommend-instance.py Qwen/Qwen2.5-3B-Instruct --deploy

  # Choose the serving tier (default auto: a fleet -> LLMDEndpoint, a single
  # replica -> VLLMEndpoint). The llm-d scale tier bakes
  # in KV/prefix/load-aware routing, with the routingProfile picked from the
  # workload (chat/rag/code/agentic -> prefix; batch/summarization -> throughput):
  ./ops/recommend-instance.py Qwen/Qwen2.5-32B-Instruct --workload agentic --users 100 --tier llm-d

  # Remove a deployed model: delete its YAML, commit, push (ArgoCD prunes it,
  # LiteLLM deregisters it). No model lookup — just the name.
  ./ops/recommend-instance.py --undeploy qwen2-5-3b-instruct"""

from .cli import main

__all__ = ["main"]
