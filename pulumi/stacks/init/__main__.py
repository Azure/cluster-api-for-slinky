# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Entrypoint for the ``ca4s-init`` PKO-owned stack.

This is the only Stack CR the outer host-side Pulumi program creates. Once PKO
reconciles it, this program instantiates control-plane and tenants/workload
resources from inside the management cluster.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_DIR = Path(__file__).resolve().parent
_PULUMI_DIR = Path(__file__).resolve().parents[2]
for path in (_PULUMI_DIR, _PROJECT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from init_stack import run


run()
