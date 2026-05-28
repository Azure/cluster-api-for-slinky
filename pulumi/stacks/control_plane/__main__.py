"""Dispatcher for the ``ca4s-control-plane`` inner Pulumi project.

Same convention as the outer ``pulumi/__main__.py``: switch on
``pulumi.get_stack()`` and import the matching per-env sibling module
by name (``control_plane_<env>.py``). PKO sets the stack name via the
third segment of ``spec.stack`` (``organization/ca4s-control-plane/<env>``),
so inside the workspace pod ``pulumi.get_stack()`` returns just
``<env>``.

Adding a new environment is purely additive — drop a new
``control_plane_<env>.py`` next to this file exposing
``def run() -> None: ...`` and it's dispatchable. No edits here.
"""

from __future__ import annotations

import importlib
import sys

import pulumi


_PROJECT_DIR = pulumi.get_root_directory()
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

# Convention: stack name ``<env>`` maps to module ``control_plane_<env>``.
# A missing module surfaces as ``ModuleNotFoundError`` from ``importlib``
# below; we rewrap that one specific failure into a clearer error so
# typos fail loudly (transitive import errors inside the dispatched
# module propagate untouched).
_STACK_MODULE_PREFIX = "control_plane_"


def main() -> None:
    stack = pulumi.get_stack()
    module_name = f"{_STACK_MODULE_PREFIX}{stack}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        raise ValueError(
            f"unsupported stack {stack!r} for ca4s-control-plane: "
            f"expected a sibling module {module_name!r} next to "
            "__main__.py. Create "
            f"pulumi/stacks/control_plane/{module_name}.py exposing "
            "``def run() -> None: ...`` to register this env."
        ) from None
    module.run()


main()
