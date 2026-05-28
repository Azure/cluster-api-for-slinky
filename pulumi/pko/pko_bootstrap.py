"""Top-level PKO bootstrap component.

Composes the building blocks in this package into a single
ComponentResource the outer stack calls once:

    PKOBootstrap
    ├── PKORelease              # Namespace + Helm OCI install
    ├── ESORelease              # External Secrets Operator (Helm)
    ├── ESOSourceAccess         # source-ns RBAC grant for ESO
    ├── StateBackend            # PVC + passphrase Secret
    ├── CredentialsProjection   # SecretStore + ExternalSecret in PKO ns
    ├── WorkspaceServiceAccount # pulumi-runner + ClusterRoleBindings
    ├── Stack/ca4s-control-plane
    └── Tenants                 # per-env fan-out → N workload-cluster Stack CRs

The control-plane Stack CR reconciles from the same repo as this
stack itself — it's how subsequent waves of management-cluster
resources (CAPI providers, AWX) get applied via PKO instead of via
further ``pulumi up`` invocations on the outer stack. Slinky CRDs +
slurm-operator + Slurm chart are NOT in this set: they live on each
tenant's workload cluster (that's where Slurm runs), and are
installed by the per-tenant workload-cluster Stack CRs. Those CRs
are emitted directly by the :class:`pko._tenants.Tenants` component
as a sibling building block; there is no intermediate
``ca4s-tenants`` mini-stack. Tenant churn happens via outer-stack
``pulumi up`` against the appropriate per-env concrete impl in
:mod:`pko._tenants_<env>`.

ESO is installed unconditionally as part of the bootstrap: it's
what keeps the GitOps credentials Secret in PKO's namespace live
against out-of-band rotation of the upstream Secret (see
:mod:`pko._credentials`). The per-source-ns RBAC grant is split out
into :class:`pko._eso_source_access.ESOSourceAccess` because it's a
per-Secret concern — if/when we project a second source Secret
(another GitOps repo, a Vault → ESO source, etc.) we instantiate
another grant + projection pair without touching the ESO install.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from pko._backend import StateBackend
from pko._credentials import CredentialsProjection
from pko._eso_release import ESORelease
from pko._eso_source_access import ESOSourceAccess
from pko._release import PKORelease
from pko._service_account import WorkspaceServiceAccount
from pko._stack_cr import StackCRSpec, build_stack_spec
from pko._tenants import Tenants


# Pulumi project name + repo dir for the control-plane inner stack.
# Kebab-case per Pulumi idiom; must match the ``name:`` field in
# ``pulumi/stacks/control_plane/Pulumi.yaml``. The workload-cluster
# project identity lives in :mod:`pko._tenants_local` (and siblings)
# because that's where workload Stack CRs are emitted.
CONTROL_PLANE_PROJECT = "ca4s-control-plane"
CONTROL_PLANE_REPO_DIR = "pulumi/stacks/control_plane/"


class PKOBootstrap(pulumi.ComponentResource):
    """Install PKO and hand off control to the inner Stack CRs.

    Args:
        name:
            Pulumi resource name. Used as a prefix for all children.
        kubeconfig:
            Output[str] kubeconfig for the management cluster.
            Drives the Kubernetes provider scoped to this component.
        repo_url:
            Output[str] in-cluster URL of the GitOps repo
            (``GitOpsRepository.url``).
        repo_branch:
            Output[str] default branch the seed push targeted
            (``GitOpsRepository.default_branch``).
        upstream_credentials_secret_name:
            Output[str] name of the upstream credentials Secret
            (``GitOpsRepository.credentials_secret_name``).
        upstream_credentials_secret_namespace:
            Output[str] namespace of the upstream credentials Secret
            (``GitOpsRepository.credentials_secret_namespace``).
        env:
            Outer-stack environment moniker (``pulumi.get_stack()``).
            Propagated to the control-plane Stack CR's ``spec.stack``
            and used by :class:`Tenants` to dispatch to the per-env
            concrete tenants impl. Plain ``str`` (not ``Input``) because
            ``Tenants`` dispatches synchronously at construction time.
        opts:
            Standard Pulumi ``ResourceOptions``.

    Outputs:
        namespace:
            PKO namespace name (constant ``pulumi-kubernetes-operator``).
        service_account:
            Workspace SA name (constant ``pulumi-runner``).
        control_plane_stack:
            ``metadata.name`` of the control-plane Stack CR (auto-named).
        workload_cluster_stacks:
            List of ``metadata.name`` of each workload-cluster Stack CR
            emitted by :class:`Tenants` (one per tenant).
    """

    namespace: Output[str]
    service_account: Output[str]
    control_plane_stack: Output[str]
    workload_cluster_stacks: list[Output[str]]

    def __init__(
        self,
        name: str,
        *,
        kubeconfig: pulumi.Input[str],
        repo_url: pulumi.Input[str],
        repo_branch: pulumi.Input[str],
        upstream_credentials_secret_name: pulumi.Input[str],
        upstream_credentials_secret_namespace: pulumi.Input[str],
        env: str,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:pko:PKOBootstrap", name, props={}, opts=opts)

        # Single Kubernetes provider scoped to the management cluster.
        # All children of this component talk through it; no ambient
        # kubeconfig dependency.
        k8s_provider = k8s.Provider(
            f"{name}-k8s",
            kubeconfig=kubeconfig,
            opts=ResourceOptions(parent=self),
        )

        release = PKORelease(
            f"{name}-release",
            provider=k8s_provider,
            opts=ResourceOptions(parent=self),
        )

        # External Secrets Operator. Installed in parallel with PKO
        # (no edge between them) since neither uses the other's CRDs
        # or runtime. CredentialsProjection takes a hard dependency
        # on this release so the SecretStore / ExternalSecret CRs
        # land after ESO's CRDs and controller are Ready.
        eso = ESORelease(
            f"{name}-eso",
            provider=k8s_provider,
            opts=ResourceOptions(parent=self),
        )

        # Per-source-ns RBAC grant for ESO: reader SA in the source
        # namespace + the TokenRequest binding to the ESO controller
        # SA. One instance per source Secret; today we only have the
        # gitea credentials. Lives between ESORelease (provides the
        # controller SA identity) and CredentialsProjection (consumes
        # the reader SA identity in its SecretStore spec).
        eso_access = ESOSourceAccess(
            f"{name}-eso-access",
            upstream_secret_name=upstream_credentials_secret_name,
            upstream_secret_namespace=upstream_credentials_secret_namespace,
            eso_namespace=eso.namespace,
            eso_controller_service_account_name=(
                eso.controller_service_account_name
            ),
            provider=k8s_provider,
            opts=ResourceOptions(parent=self, depends_on=[eso]),
        )

        backend = StateBackend(
            f"{name}-backend",
            namespace=release.namespace,
            provider=k8s_provider,
            opts=ResourceOptions(parent=self),
        )

        creds = CredentialsProjection(
            f"{name}-credentials",
            upstream_secret_name=upstream_credentials_secret_name,
            upstream_secret_namespace=upstream_credentials_secret_namespace,
            pko_namespace=release.namespace,
            reader_service_account_name=(
                eso_access.reader_service_account_name
            ),
            reader_service_account_namespace=(
                eso_access.reader_service_account_namespace
            ),
            provider=k8s_provider,
            opts=ResourceOptions(
                parent=self, depends_on=[release, eso, eso_access]
            ),
        )

        sa = WorkspaceServiceAccount(
            f"{name}-sa",
            namespace=release.namespace,
            provider=k8s_provider,
            opts=ResourceOptions(parent=self),
        )

        # Bundle the shared shape once and pass it to every Stack CR
        # build path: the control-plane CR directly here, and the
        # per-tenant workload-cluster CRs through :class:`Tenants`.
        stack_spec = StackCRSpec(
            pko_namespace=release.namespace,
            service_account_name=sa.service_account_name,
            repo_url=repo_url,
            repo_branch=repo_branch,
            credentials_secret_name=creds.projected_secret_name,
            state_pvc_name=backend.pvc_name,
            state_backend_url=backend.backend_url,
            passphrase_secret_name=backend.passphrase_secret_name,
        )

        # Everything must wait for PKO itself to be Ready. ``depends_on``
        # the release's status Output is enough — pulumi-kubernetes
        # blocks the Release on its readiness before emitting the
        # ``status`` Output.
        cr_deps: list[pulumi.Resource] = [release, creds, sa, backend]

        control_plane_spec = build_stack_spec(
            spec=stack_spec,
            project_name=CONTROL_PLANE_PROJECT,
            env=env,
            repo_dir=CONTROL_PLANE_REPO_DIR,
        )
        control_plane = k8s.apiextensions.CustomResource(
            f"{name}-control-plane",
            api_version="pulumi.com/v1",
            kind="Stack",
            metadata={"namespace": stack_spec.pko_namespace},
            spec=control_plane_spec,
            opts=ResourceOptions(
                parent=self, provider=k8s_provider, depends_on=cr_deps
            ),
        )

        # Per-env tenant fan-out. ``Tenants`` is a sibling building
        # block (like ``StateBackend``) that dispatches on ``env`` to
        # the registered concrete impl in :mod:`pko._tenants` and
        # emits one workload-cluster Stack CR per tenant. Each emitted
        # CR sets the control-plane CR as a PKO ``spec.prerequisites``
        # entry so workload reconcile blocks until the control plane
        # is reconciled.
        tenants = Tenants(
            f"{name}-tenants",
            env=env,
            stack_spec=stack_spec,
            control_plane_stack=control_plane.metadata.name,
            provider=k8s_provider,
            opts=ResourceOptions(
                parent=self, depends_on=cr_deps + [control_plane]
            ),
        )

        self.namespace = release.namespace
        self.service_account = sa.service_account_name
        self.control_plane_stack = control_plane.metadata.name
        self.workload_cluster_stacks = tenants.workload_cluster_stacks

        self.register_outputs(
            {
                "namespace": self.namespace,
                "service_account": self.service_account,
                "control_plane_stack": self.control_plane_stack,
                "workload_cluster_stacks": self.workload_cluster_stacks,
            }
        )
