"""Project entrypoint for the Kind-backed outer stack.

Pulumi requires exactly one project entrypoint (``main:`` in
``Pulumi.yaml``), which means this file.

How it works
------------
The stack body uses top-level config and local host discovery to decide which
capabilities to enable.

Stack config is read from ``Pulumi.<stack>.yaml``,
which Pulumi auto-creates on ``pulumi stack init <stack>`` and updates
on ``pulumi config set ...``.

Project layout
--------------
Cross-target shared code stays in sibling Python packages of this file:

* ``ctlptl/``  — kind / cloud-provider-kind / local image registry
    dynamic resources for Kind-backed outer stacks.
* ``gitrepo/`` — provider-agnostic ``GitOpsRepository`` ComponentResource
  contract + concrete provider impls (``gitea-builtin`` today;
  ``github`` / ``gitlab`` to follow). Reused unchanged by every target.
* ``pko/``     — Pulumi Kubernetes Operator for continuous GitOps IaC.

Adding a new capability
-----------------------
1. Add config/discovery handling to :func:`stack.run_stack`.
2. ``pulumi config set <key> <value> -s <stack>`` for any knobs.
3. ``pulumi up -s <stack>``.
"""

from __future__ import annotations

import sys

import pulumi

# Pulumi's Python language host executes this file via ``runpy``. Use the
# documented root-discovery API rather than ``__file__`` math so imports track
# ``main:`` correctly if it ever moves. See
# https://www.pulumi.com/docs/iac/concepts/projects/#root-relative-paths
_PROJECT_DIR = pulumi.get_root_directory()
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

from stack import run_stack


def main() -> None:
    """Execute the stack resource graph."""
    run_stack()


main()
