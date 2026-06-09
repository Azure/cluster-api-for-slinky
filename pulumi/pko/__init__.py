"""PKO bootstrap package.

Re-exports the single public entrypoint :class:`PKOBootstrap`. Inner building
blocks (Flux infrastructure/source helpers, PKO Helm release, state backend,
workspace SA, Stack CR spec builder, init-stack runtime, per-env tenants
fan-out) are package-internal.

Layout
------
* :mod:`pko._flux`               — Flux controllers + shared GitRepository
* :mod:`pko._release`            — Helm OCI install
* :mod:`pko._backend`            — file:// state backend (PVC + passphrase)
* :mod:`pko._service_account`    — workspace SA + ClusterRoleBindings
* :mod:`pko._init_stack`         — single init Stack CR contract + runtime
* :mod:`pko._stack_cr`           — ``build_stack_spec`` + ``StackCRSpec``
* :mod:`pko._tenants`            — per-env tenants dispatcher
* :mod:`pko._tenants_<env>`      — per-env concrete tenants impls
* :mod:`pko.pko_bootstrap`       — top-level :class:`PKOBootstrap` component
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pko.pko_bootstrap import PKOBootstrap

__all__ = ["PKOBootstrap"]


def __getattr__(name: str) -> Any:
    if name == "PKOBootstrap":
        from pko.pko_bootstrap import PKOBootstrap

        return PKOBootstrap
    raise AttributeError(name)
