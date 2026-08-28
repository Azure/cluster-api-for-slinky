# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""PKO bootstrap package.

Re-exports the single public entrypoint :class:`PKOBootstrap`. Inner building
blocks (PKO Helm release, state backend, workspace SA, Stack CR spec builder,
and init-stack runtime) are package-internal.

Layout
------
* :mod:`pko._release`            — Helm OCI install
* :mod:`pko._backend`            — file:// state backend (PVC + passphrase)
* :mod:`pko._service_account`    — workspace SA + ClusterRoleBindings
* :mod:`pko.pko_bootstrap`       — top-level :class:`PKOBootstrap` component
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pko.pko_bootstrap import PKOBootstrap

PKO_NAMESPACE = "pulumi-kubernetes-operator"

__all__ = ["PKOBootstrap", "PKO_NAMESPACE"]


def __getattr__(name: str) -> Any:
    if name == "PKOBootstrap":
        from pko.pko_bootstrap import PKOBootstrap

        return PKOBootstrap
    raise AttributeError(name)
