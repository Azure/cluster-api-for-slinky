"""Stack body for the ``local`` stack: kind + Gitea on this host.

This module is imported by ``__main__.py`` when ``pulumi.get_stack() == "local"``.
A single ``pulumi up -s local`` from the project root brings up everything
the developer loop needs, in dependency order:

1. **kind cluster + image registry + LoadBalancer support** via the
   ``ctlptl`` package (three dynamic resources wrapping ``ctlptl`` and
   ``cloud-provider-kind``).
2. **GitOps source-of-truth** via the ``gitrepo`` package — by default an
   in-cluster Gitea seeded with this repo's current ``HEAD``. Swappable
   behind a single config key for external git providers later.
3. **PKO bootstrap** — *not yet implemented*. Marked with a clear
   ``TODO`` block below. When added, the Pulumi Kubernetes Operator
   will be installed via Helm, pointed at the ``GitOpsRepository``
   outputs (URL + credentials Secret), and reconcile the cluster's
   ``Stack`` CRs from then on.

Why one stack
-------------
The ``CtlptlCluster`` Output for ``kubeconfig`` flows directly into
``GiteaBuiltinRepository``'s ``kubeconfig`` parameter as a regular
Input-typed dependency. Pulumi's Output -> Input dependency tracking then
enforces creation order (registry -> cluster -> gitea -> pko) and the
reverse on teardown, with no explicit ``depends_on`` and no out-of-band
kubeconfig surgery.

Tradeoff: one shared state blast radius. A flaky Helm install can not be
torn down independently of the cluster. Revisit (via
``pulumi.StackReference`` + a shared backend) if separate lifecycles ever
start to matter.

Sibling stack modules
---------------------
Each target environment owns its own ``stack_<name>.py``: a self-contained
Pulumi program that builds its own resources from scratch and exposes its
own ``pulumi.export(...)``s. The project-level ``__main__.py`` dispatcher
selects which one runs based on the active stack name. Cross-target shared
code (the ``ctlptl`` and ``gitrepo`` packages) stays in sibling Python
packages of this file.

When ``stack_azure.py`` gets written, expect it to reuse ``gitrepo``
directly (the GitOps contract is provider-agnostic) and replace the
Phase 1 ``ctlptl`` block with a cloud-Kubernetes equivalent.
The Phase 3 PKO bootstrap, once written, can in principle live as a
fourth sibling Python package consumed by every ``stack_<name>.py``.
"""

from __future__ import annotations

import pulumi

from ctlptl import CloudProviderKind, CtlptlCluster, CtlptlRegistry
from gitrepo import GiteaBuiltinRepository, GitOpsRepository


def run() -> None:
    """Build the ``local`` stack's resource graph and export its outputs.

    Called once by ``__main__.py``'s dispatcher. Everything below executes
    in the Pulumi language host the same way it would if this code were
    inlined into ``__main__.py`` — the wrapping is purely for clarity and
    to keep test imports of this module side-effect-free.
    """
    # ----------------------------------------------------------------------
    # Config.
    # ----------------------------------------------------------------------
    #
    # Stack-scoped config lives in ``Pulumi.local.yaml`` (auto-created by
    # ``pulumi config set ...`` on this stack). Project-scoped config keys
    # under the ``ca4s-infra:`` namespace are read here.

    config = pulumi.Config()

    # Default ``True``: on WSL2/Mac/Windows the kind docker bridge is not
    # routable from the host, so we want cloud-provider-kind to publish each
    # LoadBalancer Service via ``docker run -p 127.0.0.1:<port>:<port>`` and
    # advertise ``EXTERNAL-IP=127.0.0.1``. On a pure-Linux host with native
    # Docker, you can override to ``false`` for bridge-IP semantics:
    #     pulumi config set enable_lb_port_mapping false -s local
    enable_lb_port_mapping = config.get_bool("enable_lb_port_mapping")
    if enable_lb_port_mapping is None:
        enable_lb_port_mapping = True

    # Which GitOps provider impl to dispatch to. Keep the set of accepted
    # values narrow — silent fallback would mask typos that point at the
    # wrong git server, which is exactly the kind of subtle bug GitOps
    # stacks tend to be loud about in much less friendly ways.
    gitops_provider = config.get("gitops_provider") or "gitea-builtin"

    # ----------------------------------------------------------------------
    # Phase 1 — cluster + registry + LB controller.
    # ----------------------------------------------------------------------
    #
    # Current impl: kind (via ctlptl) + a local docker registry + the host-side
    # ``cloud-provider-kind`` daemon for LoadBalancer Services. All three
    # resources are sibling dynamic-Resource wrappers in :mod:`ctlptl`.
    #
    # This phase is intrinsically local-host-specific; the cloud equivalents
    # live in sibling ``stack_<cloud>.py`` modules (TODO: stack_azure.py
    # would use AksCluster + ACR, etc.).
    # See the dispatcher in ``__main__.py``.

    registry = CtlptlRegistry("registry")

    cluster = CtlptlCluster(
        "mgmt",
        # ``cluster_name`` omitted on purpose: the resource autonames it to
        # ``kind-mgmt-<hex>`` on first apply (and preserves the value across
        # subsequent runs). The provider also substitutes the autonamed
        # value into the manifest's ``${CLUSTER_NAME}`` placeholder; ctlptl
        # points kubectl at the new context automatically, and the
        # autonamed value is surfaced as the ``context`` stack output for
        # scripts that need it.
        #
        # Passing ``registry.registry_name`` (an ``Output[str]``) here
        # both:
        #   (a) tells the provider what to substitute for
        #       ``${REGISTRY_NAME}`` inside the manifest, and
        #   (b) wires a Pulumi DAG edge registry -> cluster, so the
        #       registry container is created first and torn down last —
        #       no explicit ``depends_on`` required.
        registry_name=registry.registry_name,
    )

    # Host-side daemon that turns ``type: LoadBalancer`` Services on kind
    # into real host-reachable IPs. The daemon is host-singleton (one
    # process services all kind clusters), so it's a sibling resource, not
    # a child of CtlptlCluster. It needs no DAG edge to the cluster — the
    # daemon polls Docker continuously and picks up new kind clusters as
    # they appear.
    lb = CloudProviderKind("lb", enable_lb_port_mapping=enable_lb_port_mapping)

    # ----------------------------------------------------------------------
    # Phase 2 — GitOps source (gitrepo).
    # ----------------------------------------------------------------------
    #
    # Passing ``cluster.kubeconfig`` directly as an Input wires the implicit
    # DAG edge cluster -> gitea, so the Gitea Helm release waits for the
    # kind cluster to be ready and tears down before the cluster on
    # destroy. No ambient kubeconfig dependency: the Kubernetes provider
    # inside ``GiteaBuiltinRepository`` reads only the bytes we hand it
    # here.
    #
    # TODO(multi-target): add cloud-hosted GitOps impls (GitHub,
    # GitLab). Each impl populates the same five-field contract
    # defined in ``gitrepo/_base.py`` (``url``, ``url_external``,
    # ``default_branch``, ``credentials_secret_name``,
    # ``credentials_secret_namespace``). Cloud impls won't need
    # ``kubeconfig`` for the *source* of truth (the git server lives
    # off-cluster) but DO need it to project the credentials Secret into
    # the management cluster's ``pulumi-kubernetes-operator`` namespace
    # — so keep the parameter on the contract.

    repo: GitOpsRepository
    if gitops_provider == "gitea-builtin":
        repo = GiteaBuiltinRepository(
            "gitops",
            kubeconfig=cluster.kubeconfig,
            # Other knobs (admin_username, repo_name, default_branch, ...)
            # keep their defaults. Surface them as config later if/when a
            # real use case for overriding shows up.
        )
    else:
        raise ValueError(
            f"unsupported ca4s-infra:gitops_provider {gitops_provider!r}; "
            "supported values: 'gitea-builtin'"
        )

    # ----------------------------------------------------------------------
    # Phase 3 — PKO bootstrap.  *** NOT YET IMPLEMENTED ***
    # ----------------------------------------------------------------------
    #
    # TODO: install the Pulumi Kubernetes Operator (PKO) into the
    # management cluster and hand off everything else to it. PKO then
    # runs three inner Pulumi programs from this same seeded repo, all
    # authored in Python (no YAML composition tier). They live as
    # sibling Pulumi projects under this same ``pulumi/`` tree, so the
    # outer dispatcher and the three inner ones can share a single
    # ``../.venv`` and a single ``pyproject``/lockfile story:
    #
    #     pulumi/stacks/control_plane/    - installs CAPI providers,
    #                                       slinky CRDs, AWX, ingress
    #                                       (mgmt-cluster operators
    #                                       only; tenant-agnostic).
    #     pulumi/stacks/tenants/          - reads a hard-coded TENANTS
    #                                       list and emits one
    #                                       ``pulumi.com/v1`` Stack CR
    #                                       per tenant pointing at
    #                                       ``pulumi/stacks/workload_cluster/``.
    #     pulumi/stacks/workload_cluster/ - per-tenant CAPI Cluster +
    #                                       slinky NodeSets;
    #                                       instantiated by the
    #                                       tenants stack, one Stack
    #                                       CR each.
    #
    # PKO Stack CR ``spec.repoDir`` values are therefore
    # ``pulumi/stacks/<name>/`` (relative to the repo root).
    #
    # Naming clarification: the *outer* program (this file, plus
    # everything directly under ``pulumi/`` excluding ``pulumi/stacks/``)
    # IS the bootstrap — it brings up kind, Gitea, and PKO from the
    # dev host. The word "bootstrap" stops here. The three inner
    # programs are named for what they ARE, not for the phase that
    # birthed them.
    #
    # Per-env dispatch in every inner program follows the same
    # ``__main__.py`` -> ``<project>_<env>.py`` trick this outer file
    # uses (see ``pulumi/__main__.py``). The env moniker propagates via
    # PKO's ``spec.stack`` field; inside the workspace pod
    # ``pulumi.get_stack()`` returns the third segment unchanged:
    #
    #     ca4s-control-plane    -> spec.stack=organization/ca4s-control-plane/local
    #                              -> dispatcher picks ``control_plane_local.py``
    #     ca4s-tenants          -> spec.stack=organization/ca4s-tenants/local
    #                              -> picks ``tenants_local.py``
    #     ca4s-workload-cluster -> spec.stack=organization/ca4s-workload-cluster/<outer_env>-<tenant>
    #                              -> dispatcher splits on first '-':
    #                                 outer_env picks the module,
    #                                 tenant is passed as a parameter.
    #
    # Constraint that falls out: outer env names (``local``, future
    # ``prod``, ...) must not contain ``-``. Tenant names may.
    #
    # Directory / project naming split:
    #   * Directories: snake_case (matches Python module imports inside
    #     each dispatcher).
    #   * Pulumi project names in ``Pulumi.yaml``: kebab-case
    #     (``ca4s-control-plane``, etc.) — idiomatic Pulumi.
    #   * Filenames within: snake_case (Python modules).
    #
    # The outer PKOBootstrap creates the FIRST TWO Stack CRs directly
    # (control-plane and tenants). Tenants gets a
    # ``spec.prerequisites: [ca4s-control-plane]`` so it waits for
    # operators to land. Workload-cluster Stack CRs are then created BY
    # the tenants stack at reconcile time — the outer never touches
    # them.
    #
    # Sketch of what this block will look like once the ``pko`` package
    # is written:
    #
    #     from pko import PKOBootstrap
    #
    #     pko = PKOBootstrap(
    #         "pko",
    #         kubeconfig=cluster.kubeconfig,
    #         repo_url=repo.url,
    #         repo_branch=repo.default_branch,
    #         upstream_credentials_secret_name=repo.credentials_secret_name,
    #         upstream_credentials_secret_namespace=repo.credentials_secret_namespace,
    #     )
    #
    # Design points already settled (don't re-litigate):
    #   * Install: Helm OCI chart
    #     ``oci://ghcr.io/pulumi/helm-charts/pulumi-kubernetes-operator``
    #     v2.3.0, namespace ``pulumi-kubernetes-operator``. Matches the
    #     Phase 2 Gitea Helm-release pattern; the quickstart manifest
    #     hard-codes a ``default/pulumi`` SA we want to override.
    #   * Per-environment Stack CRs live in the seeded repo
    #     (GitOps-purist). Only the two top-level CRs are created by
    #     this stack; workload-cluster CRs are emitted by the tenants
    #     stack at reconcile time.
    #   * Credentials projection: Pulumi-managed ``k8s.core.v1.Secret``
    #     copy in ``pulumi-kubernetes-operator`` ns, populated from the
    #     ``GitOpsRepository`` upstream Outputs. No Reflector /
    #     ExternalSecrets dep.
    #   * State backend: ``file://`` in a PVC (kind ``local-path``)
    #     mounted at ``/state`` in every workspace pod via
    #     ``spec.workspaceTemplate``. ``PULUMI_CONFIG_PASSPHRASE`` is a
    #     ``RandomPassword``-backed Secret shared across all three
    #     inner stacks. Swap to S3 / Azure Blob / Pulumi Cloud later by
    #     pointing one component input elsewhere.
    #   * Tenant enumeration: hard-coded TENANTS list in
    #     ``tenants_local.py``. Under GitOps, editing+committing a
    #     Python literal IS the operator workflow — no ConfigMap watch
    #     or external schema needed.
    #
    # Open design points NOT yet settled:
    #   * Trimming the workspace-pod ServiceAccount from
    #     ``cluster-admin`` to a least-privilege ClusterRole, once we
    #     know which providers each inner stack actually exercises.
    #     Tracked as a TODO inside ``pulumi/pko/_service_account.py``
    #     when it lands.

    # ----------------------------------------------------------------------
    # Stack outputs.
    # ----------------------------------------------------------------------
    #
    # Secrets policy: the harvested kubeconfig is intentionally NOT wrapped
    # in pulumi.Output.secret(). Rationale:
    #   * The local-filesystem backend stores stack state at .state/.pulumi/
    #     — a plain JSON file on this host. We trust the OS file permissions
    #     on that directory (chmod 700) the same way we trust
    #     ~/.kube/config's own perms.
    #   * Marking the value secret would force Pulumi to pull in
    #     passphrase-based encryption (salt in Pulumi.<stack>.yaml +
    #     PULUMI_CONFIG_PASSPHRASE), which is redundant key material
    #     guarding the same bytes the OS already gates with mode bits.
    # If this stack ever moves to a shared backend (S3, Pulumi Cloud) the
    # calculus flips — re-wrap as Output.secret and choose a real secrets
    # provider then.

    # Phase 1: cluster + registry + LB controller.
    pulumi.export("registry_name", registry.registry_name)
    pulumi.export("registry_port", registry.port)
    pulumi.export("cluster_name", cluster.cluster_name)
    pulumi.export("context", cluster.context)
    pulumi.export("kubeconfig", cluster.kubeconfig)
    pulumi.export("cloud_provider_kind_pid", lb.pid)
    pulumi.export("cloud_provider_kind_log", lb.log_path)
    pulumi.export("cloud_provider_kind_lb_port_mapping", lb.enable_lb_port_mapping)

    # Phase 2: GitOpsRepository contract — five outputs every concrete
    # provider exposes, plus an echo of which provider this run chose.
    # Don't rename without simultaneously updating the consumers (PKO,
    # dashboards, ...).
    pulumi.export("gitops_provider", gitops_provider)
    pulumi.export("gitops_url", repo.url)
    pulumi.export("gitops_url_external", repo.url_external)
    pulumi.export("gitops_default_branch", repo.default_branch)
    pulumi.export("gitops_credentials_secret_name", repo.credentials_secret_name)
    pulumi.export(
        "gitops_credentials_secret_namespace", repo.credentials_secret_namespace
    )
