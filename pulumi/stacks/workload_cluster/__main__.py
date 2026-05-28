"""Dispatcher for the ``ca4s-workload-cluster`` inner Pulumi project.

Unlike the other two inner dispatchers, this one splits the stack name
on the first ``-`` to peel off the outer env from the tenant name:

    ``<outer_env>-<tenant>`` ──split('-', 1)──> (outer_env, tenant)

``outer_env`` selects the per-env sibling module by name
(``workload_cluster_<outer_env>.py``); ``tenant`` is passed through as
a ``run(tenant=...)`` keyword argument. The split-on-first ``-`` is
why outer env names (``local``, ``prod``, ...) are forbidden from
containing ``-`` themselves — tenant names may.

Each per-env module is responsible for the actual CAPI ``Cluster``
build using ``tenant`` to namespace/label its resources. Adding a
new environment is purely additive — drop a new
``workload_cluster_<env>.py`` exposing
``def run(*, tenant: str) -> None: ...`` and it's dispatchable.
"""

from __future__ import annotations

import importlib
import sys

import pulumi


_PROJECT_DIR = pulumi.get_root_directory()
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

# Convention: outer env ``<env>`` maps to module
# ``workload_cluster_<env>``. A missing module surfaces as
# ``ModuleNotFoundError`` from ``importlib`` below; we rewrap that one
# specific failure into a clearer error so typos fail loudly
# (transitive import errors inside the dispatched module propagate
# untouched).
_STACK_MODULE_PREFIX = "workload_cluster_"


def main() -> None:
    stack = pulumi.get_stack()

    # Split on FIRST '-' only — tenant names may legally contain '-'.
    parts = stack.split("-", 1)
    if len(parts) != 2:
        raise ValueError(
            f"unexpected stack name {stack!r} for ca4s-workload-cluster; "
            "expected ``<outer_env>-<tenant>`` (e.g. ``local-example``). "
            "Workload-cluster Stack CRs are emitted by the tenants stack — "
            "if you're seeing this error, the tenants stack is producing a "
            "malformed spec.stack value."
        )
    outer_env, tenant = parts

    module_name = f"{_STACK_MODULE_PREFIX}{outer_env}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        raise ValueError(
            f"unsupported outer env {outer_env!r} (from stack {stack!r}) "
            "for ca4s-workload-cluster: expected a sibling module "
            f"{module_name!r} next to __main__.py. Create "
            f"pulumi/stacks/workload_cluster/{module_name}.py exposing "
            "``def run(*, tenant: str) -> None: ...`` to register this env."
        ) from None

    module.run(tenant=tenant)


main()
