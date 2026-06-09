"""Entrypoint for the ``ca4s-init`` PKO-owned stack.

This is the only Stack CR the outer host-side Pulumi program creates. Once PKO
reconciles it, this program instantiates control-plane and tenants/workload
resources from inside the management cluster.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pulumi

_PROJECT_DIR = Path(pulumi.get_root_directory()).resolve()
_PULUMI_DIR = _PROJECT_DIR.parents[1]
if str(_PULUMI_DIR) not in sys.path:
    sys.path.insert(0, str(_PULUMI_DIR))

from pko._init_stack import run


run()
