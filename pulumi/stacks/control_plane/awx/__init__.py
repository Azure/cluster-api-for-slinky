"""AWX install for the management cluster.

Tenant-agnostic AWX control surface. This package will grow to three
building blocks; only the operator lands in this first pass:

* :class:`AWXOperator` — Namespace + pinned ``awx-operator`` Helm
  release (installs the operator + the ``AWX`` CRD it reconciles).

Planned follow-ons (not yet implemented):

* ``AWXInstance`` — the ``AWX`` CR the operator reconciles into a
  running AWX (Deployment + Service + admin-password Secret).
* ``AWXConfiguration`` — an AWX-API provider (org + Gitea-backed
  Project + inventories) gated behind an API-readiness barrier.
"""

from __future__ import annotations

from ._operator import AWXOperator

__all__ = ["AWXOperator"]
