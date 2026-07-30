"""Pytest path setup.

The code under test lives next to the operational tooling rather than in an
installed package, so make the three import roots importable without a build
step:

  * ops/lib                                    -> sweep_env, recommend_instance/*
  * platform/services/cluster-dashboard/scripts -> fix_command_policy

Run from the repo root with: python3 -m pytest
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for rel in (
    "ops/lib",
    "platform/services/cluster-dashboard/scripts",
):
    p = os.path.join(REPO_ROOT, rel)
    if p not in sys.path:
        sys.path.insert(0, p)
