"""AWX install for the management cluster.

Tenant-agnostic AWX control surface. This package is split into
separate building blocks so the operator lifecycle, instance lifecycle,
and future API configuration can evolve independently:

* :class:`AWXOperator` — Namespace + pinned ``awx-operator`` Helm
  release (installs the operator + the ``AWX`` CRD it reconciles).
* ``AWXInstance`` — the ``AWX`` CR the operator reconciles into a
  running AWX (Deployment + Service + admin-password Secret).
* ``AWXProviderConfig`` — reads the operator-generated admin password
  and exposes the bridged AWX Pulumi provider.

Planned follow-on (not yet implemented):

* ``AWXConfiguration`` — org + Gitea-backed Project + tenant inventory
  bindings built on top of ``AWXProviderConfig``.
"""

from __future__ import annotations

from ._instance import AWXInstance
from ._operator import AWXOperator
from ._provider import AWXProviderConfig, awx_api_url, decode_secret_data_value

__all__ = [
  "AWXInstance",
  "AWXOperator",
  "AWXProviderConfig",
  "awx_api_url",
  "decode_secret_data_value",
]
