"""Project-level dispatcher: select the per-target stack body to run.

Pulumi requires exactly one project entrypoint (``main:`` in
``Pulumi.yaml``), which means this file. Per-target code lives in sibling
``stack_<name>.py`` modules so that adding a new target is purely
additive — register a new mapping below, write the module, done.

How it works
------------
``pulumi.get_stack()`` returns the active stack name (whatever follows
``pulumi up -s <stack>``). We map that name to a module name, import it,
and call its ``run()`` function. Each ``stack_<name>.py`` is responsible
for declaring resources and calling ``pulumi.export(...)`` on its
outputs.

Stack config is read by the target module from ``Pulumi.<stack>.yaml``,
which Pulumi auto-creates on ``pulumi stack init <stack>`` and updates
on ``pulumi config set ...``. The dispatcher itself reads no config.

Adding a new target
-------------------
1. ``pulumi stack init <target>`` — creates ``Pulumi.<target>.yaml``.
2. Write ``stack_<target>.py`` exposing ``def run() -> None: ...``.
3. Add ``"<target>": "stack_<target>"`` to ``_STACKS`` below.
4. ``pulumi config set <key> <value> -s <target>`` for any per-target
   knobs.
5. ``pulumi up -s <target>``.
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

# Map stack name -> module to import. Keep this dict narrow; unknown stack
# names raise so typos fail loudly instead of silently picking the wrong
# target. New targets are added here in lockstep with a new
# ``stack_<name>.py`` sibling module.
_STACKS: dict[str, str] = {
    "local": "stack_local",
    # TODO(multi-target): wire these in as their stack modules land.
    #   "azure": "stack_azure",   # AKS + ACR + Gitea-on-cluster (or GitHub)
}


def main() -> None:
    """Resolve the active stack to a module and execute its ``run()``."""
    stack = pulumi.get_stack()
    try:
        module_name = _STACKS[stack]
    except KeyError:
        raise ValueError(
            f"unsupported stack {stack!r}; "
            f"supported stacks: {sorted(_STACKS)}. "
            "Register a new stack by adding it to _STACKS in __main__.py "
            "and writing a sibling stack_<name>.py module."
        ) from None
    importlib.import_module(module_name).run()


main()
