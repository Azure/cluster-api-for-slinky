"""Pulumi program: bring up the local management cluster + registry via ctlptl.

Two dynamic resources compose the local bootstrap:

* ``CtlptlRegistry`` creates a Docker-backed image registry via ``ctlptl apply``
  with no port pin. ctlptl picks a free port using its built-in
  ``phayes/freeport`` call; we read the bound port back from
  ``ctlptl get registry <name> -o json`` and surface it as the ``port`` output.
* ``CtlptlCluster`` creates the kind cluster wired to the registry by name.
  The manifest is vendored inside the resource (see
  ``ca4s_local.ctlptl_cluster._MANIFEST_TEMPLATE``); the program no longer
  reads it from a file. ``${HOME}``, ``${CLUSTER_NAME}``, and
  ``${REGISTRY_NAME}`` are all substituted inside the provider, so
  caller-side code stays plain string + Output passing.

Pulumi's Output→Input dependency tracking enforces creation order
(registry → cluster) and reverse-order teardown (cluster → registry) so the
registry container outlives the kind cluster on destroy. No explicit
``depends_on`` needed: passing ``registry.registry_name`` as an Input is
the DAG edge.
"""

from __future__ import annotations

import pulumi

from ca4s_local import CtlptlCluster, CtlptlRegistry

# ---------------------------------------------------------------------------
# Declare the resources.
# ---------------------------------------------------------------------------

registry = CtlptlRegistry("registry")

cluster = CtlptlCluster(
    "mgmt",
    # ``cluster_name`` omitted on purpose: the resource autonames it to
    # ``kind-mgmt-<hex>`` on first apply (and preserves the value across
    # subsequent runs). The provider also substitutes the autonamed value
    # into the manifest's ``${CLUSTER_NAME}`` placeholder; ctlptl points
    # kubectl at the new context automatically, and the autonamed value is
    # surfaced as the ``context`` stack output for scripts that need it.
    #
    # Passing ``registry.registry_name`` (an ``Output[str]``) here both:
    #   (a) tells the provider what to substitute for ``${REGISTRY_NAME}``
    #       inside the manifest, and
    #   (b) wires a Pulumi DAG edge registry -> cluster, so the registry
    #       container is created first and torn down last — no explicit
    #       ``depends_on`` required.
    registry_name=registry.registry_name,
)

# ---------------------------------------------------------------------------
# Stack outputs.
# ---------------------------------------------------------------------------

# Secrets policy: the harvested kubeconfig is intentionally NOT wrapped in
# pulumi.Output.secret(). Rationale:
#   * The local-filesystem backend stores stack state at .state/.pulumi/ — a
#     plain JSON file on this host. We trust the OS file permissions on that
#     directory (chmod 700) the same way we trust ~/.kube/config's own perms.
#   * Marking the value secret would force Pulumi to pull in passphrase-based
#     encryption (salt in Pulumi.<stack>.yaml + PULUMI_CONFIG_PASSPHRASE),
#     which is redundant key material guarding the same bytes the OS already
#     gates with mode bits.
# If this stack ever moves to a shared backend (S3, Pulumi Cloud) the calculus
# flips — re-wrap as Output.secret and choose a real secrets provider then.
pulumi.export("registry_name", registry.registry_name)
pulumi.export("registry_port", registry.port)
pulumi.export("cluster_name", cluster.cluster_name)
pulumi.export("context", cluster.context)
pulumi.export("kubeconfig", cluster.kubeconfig)
