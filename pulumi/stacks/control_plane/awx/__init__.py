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
* ``AWXConfiguration`` — shared AWX organization, SCM credential, and
  GitOps-backed Project derived from PKO's Flux source.

Planned follow-on: tenant inventory bindings built on top of
``AWXConfiguration``.
"""

from __future__ import annotations

from ._configuration import (
    AWXConfiguration,
  flux_source_branch,
  flux_source_secret_name,
  flux_source_url,
  project_name_from_scm_url,
    source_control_credential_inputs,
)
from ._instance import AWXInstance
from ._operator import AWXOperator
from ._provider import AWXProviderConfig, awx_api_url, decode_secret_data_value

__all__ = [
    "AWXConfiguration",
    "AWXInstance",
    "AWXOperator",
    "AWXProviderConfig",
    "awx_api_url",
    "decode_secret_data_value",
    "flux_source_branch",
    "flux_source_secret_name",
    "flux_source_url",
    "project_name_from_scm_url",
    "source_control_credential_inputs",
]
