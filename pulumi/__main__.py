"""Project-level dispatcher: select the per-target stack body to run.

Pulumi requires exactly one project entrypoint (``main:`` in
``Pulumi.yaml``), which means this file. Per-target code lives in sibling
``stack_<name>.py`` modules so that adding a new target is purely
additive — write the module, done.

How it works
------------
``pulumi.get_stack()`` returns the active stack name (whatever follows
``pulumi up -s <stack>``). We derive the module name as ``stack_<name>``,
import it, and call its ``run()`` function. Each ``stack_<name>.py`` is
responsible for declaring resources and calling ``pulumi.export(...)``
on its outputs.

Stack config is read by the target module from ``Pulumi.<stack>.yaml``,
which Pulumi auto-creates on ``pulumi stack init <stack>`` and updates
on ``pulumi config set ...``. The dispatcher itself reads no config.

Adding a new target
-------------------
1. ``pulumi stack init <target>`` — creates ``Pulumi.<target>.yaml``.
2. Write ``stack_<target>.py`` exposing ``def run() -> None: ...``.
3. ``pulumi config set <key> <value> -s <target>`` for any per-target
   knobs.
4. ``pulumi up -s <target>``.
"""

from __future__ import annotations

import importlib
import sys

import pulumi

# Pulumi's Python language host executes this file via ``runpy``, which does
# NOT prepend the project directory to ``sys.path``. Sibling *packages*
# (``ctlptl/``, ``gitrepo/``) are found because Pulumi adds the project root
# explicitly, but sibling top-level *modules* (``stack_<name>.py``) are not
# importable until we put their directory on the path ourselves.
#
# Use the documented root-discovery API rather than ``__file__`` math so the
# value tracks ``main:`` correctly if it ever moves. See
# https://www.pulumi.com/docs/iac/concepts/projects/#root-relative-paths
_PROJECT_DIR = pulumi.get_root_directory()
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

# Map stack name -> module to import by convention: ``stack_<name>``. Keeping
# this purely functional means adding a new target requires only dropping a
# new ``stack_<name>.py`` next to this file — no edits here. Unknown stacks
# surface as a ``ModuleNotFoundError`` from ``importlib`` below, which we
# rewrap into a clearer error so typos fail loudly.
_STACK_MODULE_PREFIX = "stack_"


def main() -> None:
    """Resolve the active stack to a module and execute its ``run()``."""
    stack = pulumi.get_stack()
    module_name = f"{_STACK_MODULE_PREFIX}{stack}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # Only rewrap a failure to find the dispatch module itself; a missing
        # transitive import inside ``stack_<name>.py`` should propagate as-is.
        if exc.name != module_name:
            raise
        raise ValueError(
            f"unsupported stack {stack!r}: expected a sibling module "
            f"{module_name!r} next to __main__.py. Create "
            f"pulumi/{module_name}.py exposing ``def run() -> None: ...`` "
            "to register this stack."
        ) from None
    module.run()


main()
