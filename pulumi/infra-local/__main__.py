"""Pulumi program: bring up the local management cluster + registry + LB
controller via ctlptl + cloud-provider-kind.

Three dynamic resources compose the local bootstrap:

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
* ``CloudProviderKind`` spawns the ``cloud-provider-kind`` host daemon
  which turns ``type: LoadBalancer`` Services on kind into real
  host-reachable IPs. Host-singleton (one daemon services every kind
  cluster on this host); no DAG edge to the cluster is needed because
  the daemon polls Docker and picks up clusters as they appear.

Pulumi's Output→Input dependency tracking enforces creation order
(registry → cluster) and reverse-order teardown (cluster → registry) so the
registry container outlives the kind cluster on destroy. No explicit
``depends_on`` needed: passing ``registry.registry_name`` as an Input is
the DAG edge. ``CloudProviderKind`` is independent — it has no Outputs
that anyone else consumes and creates/destroys in parallel with the rest.
"""

from __future__ import annotations

import pulumi

from ca4s_local import CloudProviderKind, CtlptlCluster, CtlptlRegistry

# ---------------------------------------------------------------------------
# Config.
# ---------------------------------------------------------------------------

_config = pulumi.Config()
# Default ``True``: on WSL2/Mac/Windows the kind docker bridge is not
# routable from the host, so we want cloud-provider-kind to publish each
# LoadBalancer Service via ``docker run -p 127.0.0.1:<port>:<port>`` and
# advertise ``EXTERNAL-IP=127.0.0.1``. On a pure-Linux host with native
# Docker, you can override to ``false`` for bridge-IP semantics:
#     pulumi config set enable_lb_port_mapping false
_enable_lb_port_mapping = _config.get_bool("enable_lb_port_mapping")
if _enable_lb_port_mapping is None:
    _enable_lb_port_mapping = True

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

# Host-side daemon that turns ``type: LoadBalancer`` Services on kind into
# real host-reachable IPs. The daemon is host-singleton (one process
# services all kind clusters), so it's a sibling resource, not a child of
# CtlptlCluster. It needs no DAG edge to the cluster — the daemon polls
# Docker continuously and picks up new kind clusters as they appear.
lb = CloudProviderKind("lb", enable_lb_port_mapping=_enable_lb_port_mapping)

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
pulumi.export("cloud_provider_kind_pid", lb.pid)
pulumi.export("cloud_provider_kind_log", lb.log_path)
pulumi.export("cloud_provider_kind_lb_port_mapping", lb.enable_lb_port_mapping)
