#!/usr/bin/env python3
"""Thin executable shim for the recommend_instance package.

Right-size EC2 GPU instances for serving an LLM on this EKS platform. The
implementation lives in the recommend_instance/ package (one module per
concern: catalog, pricing, model, vram, throughput, recommend, scaling,
render, cli). See recommend_instance/__init__.py for the full usage docs, or
run with --help.

Invoked via `./platformctl new-model <model> [flags]`; also runnable directly as
`./ops/lib/recommend-instance.py <model> [flags]`.
"""

import os
import sys

# Allow running as a standalone script (python3 ops/recommend-instance.py ...)
# by making the package importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from recommend_instance import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
