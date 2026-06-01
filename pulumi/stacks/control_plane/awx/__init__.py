"""AWX install for the management cluster.

Tenant-agnostic AWX control surface. This package is split into
separate building blocks so the operator lifecycle, instance lifecycle,
and future API configuration can evolve independently:

* :class:`AWXOperator` — Namespace + pinned ``awx-operator`` Helm
  release (installs the operator + the ``AWX`` CRD it reconciles).
* ``AWXInstance`` — the ``AWX`` CR the operator reconciles into a
  running AWX (Deployment + Service + admin-password Secret).

Planned follow-on (not yet implemented):

* ``AWXConfiguration`` — an AWX-API provider (org + Gitea-backed
  Project + inventories) gated behind an API-readiness barrier.
"""

from __future__ import annotations

from ._instance import AWXInstance
from ._operator import AWXOperator

__all__ = ["AWXInstance", "AWXOperator"]
