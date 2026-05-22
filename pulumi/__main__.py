"""Pulumi program: end-to-end local bootstrap for the management cluster.

This is the umbrella entrypoint. A single ``pulumi up`` from this project
brings up everything the developer loop needs, in dependency order:

1. **kind cluster + image registry + LoadBalancer support** via the
   ``ctlptl`` package (three dynamic resources wrapping ``ctlptl`` and
   ``cloud-provider-kind``).
2. **GitOps source-of-truth** via the ``gitrepo`` package — by default an
   in-cluster Gitea seeded with this repo's current ``HEAD``. Swappable
   behind a single config key for external git providers later.
3. **Flux bootstrap** — *not yet implemented*. Marked with a clear
   ``TODO`` block below. When added, Flux will be installed via Helm,
   pointed at the ``GitOpsRepository`` outputs (URL + credentials Secret),
   and reconcile the cluster from then on.

Why one stack instead of two?
-----------------------------
Earlier in this project's life the cluster bootstrap and the gitops bring-up
lived in two separate Pulumi projects with separate state files. That setup
forced us to rely on ambient ``~/.kube/config`` context to wire them
together — fragile if anything ``kubectl config use-context``-ed in between
the two ``pulumi up`` invocations.

Folding both into a single stack lets the ``CtlptlCluster`` Output for
``kubeconfig`` flow directly into ``GiteaBuiltinRepository``'s
``kubeconfig`` parameter as a regular Input-typed dependency. Pulumi's
Output -> Input dependency tracking then enforces creation order
(registry -> cluster -> gitea -> flux) and the reverse on teardown, with
no explicit ``depends_on`` and no out-of-band kubeconfig surgery.

Tradeoff: one shared state blast radius. A flaky Helm install can no longer
be torn down independently of the cluster. Worth it for the simpler wiring;
revisit (via ``pulumi.StackReference`` + a shared backend) if separate
lifecycles ever start to matter.

Package layout
--------------
* :mod:`ctlptl` at ``pulumi/ctlptl/`` — kind / cloud-provider-kind / local
  image registry dynamic resources. Sibling of this file.
* :mod:`gitrepo` at ``pulumi/gitrepo/`` — GitOpsRepository ComponentResource
  contract + the ``gitea-builtin`` implementation. Sibling of this file.

Both packages are auto-importable because Pulumi adds the project root
(``pulumi/``) to ``sys.path`` for the language host AND for every
dynamic-Resource worker subprocess. No editable install, no ``sys.path``
shim, no ``PYTHONPATH``. If you ever split this entrypoint into multiple
sibling projects (e.g. ``pulumi/local/Pulumi.yaml`` and
``pulumi/aws/Pulumi.yaml``) that need to share these packages, graduate
to a ``pulumi/libs/`` directory with a one-line ``pyproject.toml`` +
``-e ./pulumi/libs`` in ``requirements.txt``.

TODO(multi-target): future generalization across local/cloud
-------------------------------------------------------------
This entrypoint currently hardcodes "kind on the host" as the cluster
provider. To extend without forking the file:

* **Cluster provider dispatch.** Add a ``cluster_provider`` config key
  mirroring the existing ``gitops_provider`` pattern. Phase 1 then
  dispatches between local impls (``kind-ctlptl`` — current) and cloud
  impls (``eks``, ``gke``, ``aks``). Each impl emits the same contract:
  ``cluster_name``, ``context``, ``kubeconfig`` (Output[str]),
  ``registry_name`` / ``registry_port`` (or ``None`` if the cluster uses
  an external registry like ECR/Artifact Registry/ACR).

* **Gitops provider dispatch.** Already in place via ``gitops_provider``.
  Cloud impls to add when needed: ``github`` (PAT-auth), ``gitlab``,
  ``codecommit`` (IAM-auth). The contract (``url``, ``url_external``,
  ``default_branch``, ``credentials_secret_*``) is already provider-
  agnostic — see ``gitrepo/_base.py``.

* **Flux phase parametrization.** Different cloud platforms favor
  different Flux install paths (raw Helm vs. EKS-Anywhere bundle vs.
  GKE Config Sync alternative). The flux phase, when written, should
  dispatch on a ``flux_provider`` config key.

* **Project name / state separation.** Once the above lands, drop
  ``-local`` from the project name (just ``ca4s-infra``) and use stack
  config — not the project name — to distinguish targets. Stack names
  like ``dev-local-kind``, ``dev-aws-eks``, ``prod-aws-eks`` carry the
  variant in the place stack config naturally lives.
"""

from __future__ import annotations

import pulumi

from ctlptl import CloudProviderKind, CtlptlCluster, CtlptlRegistry
from gitrepo import GiteaBuiltinRepository, GitOpsRepository

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

# TODO(multi-target): once we add EKS / GKE / AKS impls, introduce a
# ``cluster_provider`` config key here that selects between local
# (``kind-ctlptl`` — current behavior, baked into Phase 1 below) and
# cloud impls. Mirror the dispatch shape used by ``gitops_provider``
# below — narrow set of accepted values, raise on unknown, default to
# the local impl so existing dev loops don't break.
_cluster_provider = "kind-ctlptl"  # placeholder; not yet read from config

# Which GitOps provider impl to dispatch to. Keep the set of accepted
# values narrow — silent fallback would mask typos that point at the
# wrong git server, which is exactly the kind of subtle bug GitOps
# stacks tend to be loud about in much less friendly ways.
_gitops_provider = _config.get("gitops_provider") or "gitea-builtin"


# ---------------------------------------------------------------------------
# Phase 1 — cluster + registry + LB controller.
# ---------------------------------------------------------------------------
#
# Current impl: kind (via ctlptl) + a local docker registry + the host-side
# ``cloud-provider-kind`` daemon for LoadBalancer Services. All three
# resources are sibling dynamic-Resource wrappers in :mod:`ctlptl`.
#
# TODO(multi-target): when adding cloud impls, refactor this block into a
# ``cluster_provider`` dispatch::
#
#     if _cluster_provider == "kind-ctlptl":
#         registry = CtlptlRegistry("registry")
#         cluster = CtlptlCluster("mgmt", registry_name=registry.registry_name)
#         lb = CloudProviderKind("lb", enable_lb_port_mapping=...)
#     elif _cluster_provider == "eks":
#         cluster = EksCluster("mgmt", ...)
#         registry = None  # cloud envs typically rely on ECR/etc, not a sibling
#         lb = None        # AWS LB controller is part of cluster bringup
#     elif _cluster_provider == "gke": ...
#
# Each impl returns the same minimal contract (``cluster_name``, ``context``,
# ``kubeconfig``) plus optional registry / lb handles. The outputs block at
# the bottom then conditionally exports the local-only fields.

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
# Phase 2 — GitOps source (gitrepo).
# ---------------------------------------------------------------------------
#
# Passing ``cluster.kubeconfig`` directly as an Input wires the implicit
# DAG edge cluster -> gitea, so the Gitea Helm release waits for the kind
# cluster to be ready and tears down before the cluster on destroy. No
# ambient kubeconfig dependency: the Kubernetes provider inside
# ``GiteaBuiltinRepository`` reads only the bytes we hand it here.
#
# TODO(multi-target): add cloud-hosted GitOps impls (GitHub, GitLab,
# CodeCommit). Each impl populates the same five-field contract defined
# in ``gitrepo/_base.py`` (``url``, ``url_external``, ``default_branch``,
# ``credentials_secret_name``, ``credentials_secret_namespace``). Cloud
# impls won't need ``kubeconfig`` for the *source* of truth (the git
# server lives off-cluster) but DO need it to project the credentials
# Secret into the management cluster's flux-system namespace — so keep
# the parameter on the contract.

repo: GitOpsRepository
if _gitops_provider == "gitea-builtin":
    repo = GiteaBuiltinRepository(
        "gitops",
        kubeconfig=cluster.kubeconfig,
        # Other knobs (admin_username, repo_name, default_branch, ...) keep
        # their defaults. Surface them as config later if/when a real use
        # case for overriding shows up.
    )
else:
    raise ValueError(
        f"unsupported ca4s-infra-local:gitops_provider {_gitops_provider!r}; "
        "supported values: 'gitea-builtin'"
    )


# ---------------------------------------------------------------------------
# Phase 3 — Flux bootstrap.  *** NOT YET IMPLEMENTED ***
# ---------------------------------------------------------------------------
#
# TODO: install Flux into the management cluster and point it at the
# GitOpsRepository above. Sketch of what this block will look like once
# the ``flux`` package is written:
#
#     from flux import FluxBootstrap
#
#     flux = FluxBootstrap(
#         "flux",
#         kubeconfig=cluster.kubeconfig,
#         repo_url=repo.url,
#         repo_branch=repo.default_branch,
#         credentials_secret_name=repo.credentials_secret_name,
#         credentials_secret_namespace=repo.credentials_secret_namespace,
#     )
#
# Open design questions to settle when implementing:
#   * Helm chart vs. ``flux bootstrap`` CLI vs. raw manifest application?
#     Helm chart gives Pulumi a real resource to track; the CLI is the
#     upstream-blessed path but is harder to model.
#   * Where do GitRepository/Kustomization CRs live — managed here, or
#     committed into the seeded repo so they become self-reconciling?
#     The latter is the GitOps purist answer.
#   * If credentials live in the ``gitea`` namespace and Flux lives in
#     ``flux-system``, who copies the Secret across? (Reflector? Pulumi
#     k8s.core.v1.Secret with a manual ``data=`` copy? ExternalSecrets?)


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

# Phase 1: cluster + registry + LB controller.
pulumi.export("registry_name", registry.registry_name)
pulumi.export("registry_port", registry.port)
pulumi.export("cluster_name", cluster.cluster_name)
pulumi.export("context", cluster.context)
pulumi.export("kubeconfig", cluster.kubeconfig)
pulumi.export("cloud_provider_kind_pid", lb.pid)
pulumi.export("cloud_provider_kind_log", lb.log_path)
pulumi.export("cloud_provider_kind_lb_port_mapping", lb.enable_lb_port_mapping)

# Phase 2: GitOpsRepository contract — five outputs every concrete provider
# exposes, plus an echo of which provider this run chose. Don't rename
# without simultaneously updating the consumers (Flux, dashboards, ...).
pulumi.export("gitops_provider", _gitops_provider)
pulumi.export("gitops_url", repo.url)
pulumi.export("gitops_url_external", repo.url_external)
pulumi.export("gitops_default_branch", repo.default_branch)
pulumi.export("gitops_credentials_secret_name", repo.credentials_secret_name)
pulumi.export("gitops_credentials_secret_namespace", repo.credentials_secret_namespace)
