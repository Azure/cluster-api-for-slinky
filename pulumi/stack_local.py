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
3. **Flux source handoff + PKO bootstrap** via the ``pko`` package — installs
    Flux source/notification controllers, declares a Flux ``GitRepository`` for
    the hydrated GitOps repo, installs the Pulumi Kubernetes Operator (Helm OCI),
    and emits exactly one ``pulumi.com/v1`` Stack CR: ``ca4s-init``. From there
    on PKO owns reconcile, including the control-plane Stack CR and
    tenant/workload resources that the init stack creates reflexively.

Why one stack
-------------
The ``CtlptlCluster`` Output for ``kubeconfig`` flows directly into the selected
``GitOpsRepository`` provider's ``kubeconfig`` parameter as a regular
Input-typed dependency. Pulumi's Output -> Input dependency tracking then
enforces creation order (registry -> cluster -> gitea -> pko) and the reverse on
teardown, with no explicit ``depends_on`` and no out-of-band kubeconfig surgery.

Tradeoff: one shared state blast radius. A flaky Helm install can not be
torn down independently of the cluster. Revisit (via
``pulumi.StackReference`` + a shared backend) if separate lifecycles ever
start to matter.

For the dispatcher conventions (one ``stack_<name>.py`` per target,
shared ``ctlptl`` / ``gitrepo`` packages, future ``stack_azure.py``
layout) see the "Project layout" section of ``__main__.py``.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s

from ctlptl import CloudProviderKind, CtlptlCluster, CtlptlRegistry
from gitrepo import GitOpsRepository, GitOpsWebhook
from pko import PKOBootstrap
from pko._flux import FluxInfrastructure
from pko._release import PKO_NAMESPACE
from stacks.workload_cluster.registry_setting import (
    REGISTRY_CONFIG_KEY,
    local_port_registry_setting,
)


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

    # Operator-controlled replacement inputs for the one-shot Gitea sync.
    # Example:
    #     pulumi config set --path 'gitea_sync_triggers.generation' rerun-1 -s local
    # Bump any key/value to force a normal non-force push without changing HEAD.
    gitea_sync_triggers = config.get_object("gitea_sync_triggers") or {}
    configured_gitops_provider_args = config.get_object("gitops_provider_args") or {}

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

    mgmt_provider = k8s.Provider(
        "mgmt-k8s",
        kubeconfig=cluster.kubeconfig,
    )

    pko_namespace = k8s.core.v1.Namespace(
        "pko-ns",
        metadata={"name": PKO_NAMESPACE},
        opts=pulumi.ResourceOptions(
            provider=mgmt_provider,
        ),
    )

    # Host-side daemon that turns ``type: LoadBalancer`` Services on kind
    # into real host-reachable IPs. The daemon is host-singleton (one
    # process services all kind clusters), so it's a sibling resource, not
    # a child of CtlptlCluster. It needs no DAG edge to the cluster — the
    # daemon polls Docker continuously and picks up new kind clusters as
    # they appear.
    lb = CloudProviderKind("lb", enable_lb_port_mapping=enable_lb_port_mapping)

    flux = FluxInfrastructure(
        "flux",
        provider=mgmt_provider,
    )

    # ----------------------------------------------------------------------
    # Phase 2 — GitOps source (gitrepo).
    # ----------------------------------------------------------------------
    #
    # Passing ``cluster.kubeconfig`` directly as an Input wires the implicit
    # DAG edge cluster -> gitea, so the Gitea Helm release waits for the
    # kind cluster to be ready and tears down before the cluster on
    # destroy. No ambient kubeconfig dependency: the Kubernetes provider
    # inside the selected GitOpsRepository provider reads only the bytes we hand
    # it here.
    #
    # TODO(multi-target): add cloud-hosted GitOps impls (GitHub,
    # GitLab). Each impl populates the same GitOpsRepository contract
    # defined in ``gitrepo/_base.py`` (``url``, ``url_external``,
    # ``default_branch``, ``ssh_private_key_secret``, ``ssh_known_hosts``).
    # Cloud impls won't need ``kubeconfig`` for the *source* of truth (the git
    # server lives off-cluster) but DO need credentials projected into the
    # management cluster so Flux source-controller can produce artifacts for PKO.

    repo = GitOpsRepository(
        "gitops",
        gitops_provider_name=gitops_provider,
        gitops_provider_args={
            **configured_gitops_provider_args,
            "kubeconfig": cluster.kubeconfig,
            "flux_provider": mgmt_provider,
            "flux_infrastructure": flux,
            "pko_namespace_resource": pko_namespace,
            "sync_triggers": gitea_sync_triggers,
        },
    )

    # ----------------------------------------------------------------------
    # Phase 3 — PKO bootstrap.
    # ----------------------------------------------------------------------
    #
    # Install PKO and hand it the Flux GitRepository source created by the
    # GitOps provider. The outer stack owns exactly one Stack CR under PKO:
    # ``ca4s-init``. That init stack runs inside PKO, creates the control-plane
    # Stack CR, waits for it to become ready, then instantiates tenant/workload
    # resources directly. Workload-cluster instance churn is therefore
    # reconciled by PKO from Git after the init stack notices the repo change,
    # rather than by adding/removing Stack CRs directly from this outer
    # host-side graph.
    #
    # Two inner Pulumi projects live as sibling directories under
    # ``pulumi/stacks/``, sharing the outer ``../../../.venv``; tenant/workload
    # code is imported as a component module by the init stack:
    #
    #     pulumi/stacks/init/             - the single outer-owned PKO Stack;
    #                                       reflexively emits child Stack CRs.
    #     pulumi/stacks/control_plane/    - CAPI providers + AWX
    #                                       (mgmt-cluster operators
    #                                       only; tenant-agnostic).
    #     pulumi/stacks/workload_cluster/ - tenant/workload component modules;
    #                                       ``TenantLocal`` creates per-instance
    #                                       CAPI Clusters on mgmt, then installs
    #                                       workload-cluster-side resources via
    #                                       each cluster's own kubeconfig.
    #
    # Per-env dispatch in every inner program follows the same
    # ``__main__.py`` -> ``<project>_<env>.py`` trick this outer file
    # uses (see ``pulumi/__main__.py``). The env moniker propagates via
    # PKO's ``spec.stack`` field; inside the workspace pod
    # ``pulumi.get_stack()`` returns the third segment unchanged:
    #
    #     ca4s-init             -> spec.stack=organization/ca4s-init/local
    #                              -> creates the child Stack CRs below
    #     ca4s-control-plane    -> spec.stack=organization/ca4s-control-plane/local
    #                              -> dispatcher picks ``control_plane_local.py``
    #     TenantLocal            -> component instantiated by ca4s-init after
    #                              the control-plane Stack CR is ready. It owns
    #                              ``spec.workloadClusters`` inventory, instance
    #                              fan-out, and cross-workload-cluster concerns.

    pko = PKOBootstrap(
        "pko",
        provider=mgmt_provider,
        namespace_resource=pko_namespace,
        flux_source_name=repo.flux_source_name,
        flux_source_resource=repo.flux_source,
        env=pulumi.get_stack(),
        config={REGISTRY_CONFIG_KEY: local_port_registry_setting(registry.port)},
    )

    gitops_webhook = GitOpsWebhook(
        "gitops-flux-webhook",
        gitops_provider_name=gitops_provider,
        gitops_webhook_args=repo.webhook_args,
        opts=pulumi.ResourceOptions(depends_on=[pko]),
    )

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

    # Phase 2: GitOpsRepository contract — outputs every concrete
    # provider exposes, plus an echo of which provider this run chose.
    # Don't rename without simultaneously updating the consumers (PKO,
    # dashboards, ...).
    pulumi.export("gitops_provider", gitops_provider)
    pulumi.export("gitops_url", repo.url)
    pulumi.export("gitops_url_external", repo.url_external)
    pulumi.export("gitops_default_branch", repo.default_branch)
    pulumi.export("gitops_ssh_known_hosts", repo.ssh_known_hosts)
    pulumi.export(
        "gitops_ssh_private_key_secret_name",
        repo.ssh_private_key_secret.metadata["name"],
    )
    pulumi.export(
        "gitops_ssh_private_key_secret_namespace",
        repo.ssh_private_key_secret.metadata["namespace"],
    )

    # Phase 3: PKO bootstrap handles + the one outer-owned init Stack CR name.
    # The control-plane and workload-cluster Stack CR names are outputs of the
    # PKO-owned init stack, not this host-side outer stack.
    pulumi.export("pko_namespace", pko.namespace)
    pulumi.export("pko_service_account", pko.service_account)
    pulumi.export("pko_flux_source_name", repo.flux_source_name)
    pulumi.export("pko_flux_receiver_url", repo.flux_receiver_url)
    pulumi.export("gitops_flux_webhook_id", gitops_webhook.hook_id)
    pulumi.export("pko_init_stack", pko.init_stack)
