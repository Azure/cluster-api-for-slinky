"""PKO bootstrap package.

Re-exports the single public entrypoint :class:`PKOBootstrap`. Inner
building blocks (state backend, credentials projection, workspace SA,
Stack CR spec builder, per-env tenants fan-out) are package-internal
and wired together by :class:`PKOBootstrap` itself.

Layout
------
* :mod:`pko._release`            — Helm OCI install + namespace
* :mod:`pko._eso_release`        — External Secrets Operator (Helm)
* :mod:`pko._eso_source_access`  — per-source-ns RBAC grant for ESO
* :mod:`pko._backend`            — file:// state backend (PVC + passphrase)
* :mod:`pko._credentials`        — ESO-backed cross-ns git credentials sync
* :mod:`pko._service_account`    — workspace SA + ClusterRoleBindings
* :mod:`pko._stack_cr`           — ``build_stack_spec`` + ``StackCRSpec``
* :mod:`pko._tenants`            — per-env tenants dispatcher
* :mod:`pko._tenants_<env>`      — per-env concrete tenants impls
* :mod:`pko.pko_bootstrap`       — top-level :class:`PKOBootstrap` component
"""

from __future__ import annotations

from pko.pko_bootstrap import PKOBootstrap

__all__ = ["PKOBootstrap"]
