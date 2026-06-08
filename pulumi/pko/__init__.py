"""PKO bootstrap package.

Re-exports the single public entrypoint :class:`PKOBootstrap`. Inner
building blocks (PKO Helm release with SSH mount, state backend,
workspace SA, Stack CR spec builder, init-stack runtime, per-env tenants
fan-out) are package-internal.

Layout
------
* :mod:`pko._release`            — Helm OCI install + namespace + SSH mount
* :mod:`pko._backend`            — file:// state backend (PVC + passphrase)
* :mod:`pko._service_account`    — workspace SA + ClusterRoleBindings
* :mod:`pko._init_stack`         — single init Stack CR contract + runtime
* :mod:`pko._stack_cr`           — ``build_stack_spec`` + ``StackCRSpec``
* :mod:`pko._tenants`            — per-env tenants dispatcher
* :mod:`pko._tenants_<env>`      — per-env concrete tenants impls
* :mod:`pko.pko_bootstrap`       — top-level :class:`PKOBootstrap` component
"""

from __future__ import annotations

__all__ = ["PKOBootstrap"]


def __getattr__(name: str) -> object:
	if name == "PKOBootstrap":
		from pko.pko_bootstrap import PKOBootstrap

		return PKOBootstrap
	raise AttributeError(name)
