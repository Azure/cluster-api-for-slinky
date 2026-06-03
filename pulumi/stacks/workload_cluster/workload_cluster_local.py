"""Dispatcher for the ``local`` outer env of ``ca4s-workload-cluster``.

The workload-cluster project stack name is ``<outer_env>-<tenant>``. The
top-level ``__main__.py`` peels off ``outer_env`` and imports this module for
``local`` stacks. This module then dispatches to a concrete per-env/per-tenant
sibling module named ``workload_cluster_local_<tenant_module>.py``.

The extra dispatch layer keeps tenant resource graphs independent. Local today
has one tenant, ``example``, but a future tenant can have a substantially
different CAPI/Slurm shape by adding a new sibling module rather than growing
conditionals here.
"""

from __future__ import annotations

import importlib
import re


_TENANT_MODULE_PREFIX = "workload_cluster_local_"
_TENANT_MODULE_INVALID_CHARS = re.compile(r"[^a-z0-9_]+")


def _tenant_module_suffix(tenant: str) -> str:
    suffix = _TENANT_MODULE_INVALID_CHARS.sub("_", tenant.lower()).strip("_")
    if not suffix:
        raise ValueError("tenant must contain at least one module-safe character")
    if suffix[0].isdigit():
        suffix = f"tenant_{suffix}"
    return suffix


def run(tenant: str) -> None:
    """Dispatch a local workload-cluster stack to its tenant module."""
    tenant_module_suffix = _tenant_module_suffix(tenant)
    module_name = f"{_TENANT_MODULE_PREFIX}{tenant_module_suffix}"

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        raise ValueError(
            f"unsupported local workload tenant {tenant!r}: expected sibling "
            f"module {module_name!r}. Create "
            f"pulumi/stacks/workload_cluster/{module_name}.py exposing "
            "``def run() -> None: ...`` to register this tenant."
        ) from None

    module.run()
