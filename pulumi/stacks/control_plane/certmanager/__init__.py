# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""cert-manager install for the management cluster.

cert-manager is a foundational dependency of the control plane: the
upcoming ``cluster-api-operator`` chart (and CAPI provider webhooks)
require it for serving/validating webhook certificates, and AWX can
use it for ingress TLS. It is installed once here, tenant-agnostic,
ahead of everything that needs admission webhooks.

Public surface:

* :class:`CertManager` — Namespace + pinned ``cert-manager`` Helm
  release (CRDs included).
"""

from __future__ import annotations

from ._release import CertManager

__all__ = ["CertManager"]
