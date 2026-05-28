"""Top-level PKO bootstrap component.

Composes the building blocks in this package into a single
ComponentResource the outer stack calls once:

    PKOBootstrap
    ├── Namespace/pulumi-kubernetes-operator
    ├── Secret/gitea-ssh             # admin SSH private key + known_hosts
    ├── PKORelease                   # Helm OCI install w/ SSH Secret mount
    ├── StateBackend                 # PVC + passphrase Secret
    ├── WorkspaceServiceAccount      # pulumi-runner + ClusterRoleBindings
    ├── Stack/ca4s-control-plane
    └── Tenants                      # per-env fan-out → N workload-cluster Stack CRs

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

The SSH path replaced the previous Flux + ESO indirection. PKO's
``projectRepo`` only accepts ``https`` and ``ssh`` schemes; our
in-cluster Gitea ships no TLS, so SSH is the only viable scheme.
The repo component (:mod:`gitrepo.gitea_builtin`) generates an
admin ed25519 keypair AND a Gitea host keypair deterministically
from ``RandomBytes`` seeds; the public user-key is uploaded to
Gitea via the REST API, the private user-key + the known_hosts
entry derived from the host pubkey both land in a single Secret
(``gitea-ssh``) in PKO's namespace via the projection below.
:mod:`pko._release` and :mod:`pko._stack_cr` both mount that same
Secret \u2014 the operator pod uses it for the controller-side
``git ls-remote``; the workspace pod uses it for the ``git clone``
the inner stack's first step performs.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from pko._backend import StateBackend
from pko._release import PKO_NAMESPACE, PKORelease
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

# Name of the Secret PKOBootstrap projects into PKO's namespace
# holding the admin SSH credentials. The two keys it carries
# (private key + known_hosts) are referenced by both
# :mod:`pko._release` (operator pod mount) and :mod:`pko._stack_cr`
# (workspace pod mount + Stack CR ``gitAuth.sshAuth.sshPrivateKey``).
_SSH_SECRET_NAME = "gitea-ssh"
_SSH_PRIVATE_KEY_KEY = "id_ed25519"
_SSH_KNOWN_HOSTS_KEY = "known_hosts"


class PKOBootstrap(pulumi.ComponentResource):
    """Install PKO and hand off control to the inner Stack CRs.

    Args:
        name:
            Pulumi resource name. Used as a prefix for all children.
        kubeconfig:
            Output[str] kubeconfig for the management cluster.
            Drives the Kubernetes provider scoped to this component.
        repo_url:
            Output[str] in-cluster SSH URL of the GitOps repo
            (``GitOpsRepository.url``).
        repo_branch:
            Output[str] default branch the seed push targeted
            (``GitOpsRepository.default_branch``).
        ssh_private_key:
            Secret-marked Output[str] OpenSSH PEM admin private key
            (``GitOpsRepository.ssh_private_key``). Projected into the
            ``gitea-ssh`` Secret under key ``id_ed25519``.
        ssh_known_hosts:
            Output[str] single-line ``known_hosts`` entry for the SSH
            endpoint (``GitOpsRepository.ssh_known_hosts``). Projected
            into the same Secret under key ``known_hosts``.
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
        ssh_private_key: pulumi.Input[str],
        ssh_known_hosts: pulumi.Input[str],
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

        # Namespace owned here (not in PKORelease) so the SSH Secret
        # below can be created in the same ns BEFORE the Helm install
        # references it. The chart install is ``atomic=True``, so a
        # missing Secret at install time would cause a rollback.
        ns = k8s.core.v1.Namespace(
            f"{name}-ns",
            metadata={"name": PKO_NAMESPACE},
            opts=ResourceOptions(parent=self, provider=k8s_provider),
        )

        # The single Secret PKO reads admin SSH credentials from. Two
        # keys: ``id_ed25519`` (the private key, secret-marked at the
        # repo component's output) and ``known_hosts`` (the host-key
        # entry derived deterministically from the Gitea host keypair).
        # Mounted into the operator pod by :mod:`pko._release` and into
        # workspace pods by :mod:`pko._stack_cr`; referenced by the
        # Stack CR's ``gitAuth.sshAuth.sshPrivateKey``.
        ssh_secret = k8s.core.v1.Secret(
            f"{name}-ssh",
            metadata={
                "name": _SSH_SECRET_NAME,
                "namespace": PKO_NAMESPACE,
            },
            type="Opaque",
            string_data={
                _SSH_PRIVATE_KEY_KEY: ssh_private_key,
                _SSH_KNOWN_HOSTS_KEY: ssh_known_hosts,
            },
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[ns],
            ),
        )

        release = PKORelease(
            f"{name}-release",
            provider=k8s_provider,
            namespace_resource=ns,
            ssh_secret_name=_SSH_SECRET_NAME,
            ssh_secret_resource=ssh_secret,
            opts=ResourceOptions(parent=self),
        )

        backend = StateBackend(
            f"{name}-backend",
            namespace=release.namespace,
            provider=k8s_provider,
            opts=ResourceOptions(parent=self),
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
            ssh_secret_name=_SSH_SECRET_NAME,
            state_pvc_name=backend.pvc_name,
            state_backend_url=backend.backend_url,
            passphrase_secret_name=backend.passphrase_secret_name,
        )

        # Everything must wait for PKO itself to be Ready. ``depends_on``
        # the release's status Output is enough — pulumi-kubernetes
        # blocks the Release on its readiness before emitting the
        # ``status`` Output.
        cr_deps: list[pulumi.Resource] = [release, ssh_secret, sa, backend]

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
            metadata={"namespace": PKO_NAMESPACE},
            spec=control_plane_spec,
            opts=ResourceOptions(
                parent=self, provider=k8s_provider, depends_on=cr_deps
            ),
        )

        control_plane_metadata_name = control_plane.metadata["name"]

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
            control_plane_stack=control_plane_metadata_name,
            provider=k8s_provider,
            opts=ResourceOptions(
                parent=self, depends_on=cr_deps + [control_plane]
            ),
        )

        self.namespace = release.namespace
        self.service_account = sa.service_account_name
        self.control_plane_stack = control_plane_metadata_name
        self.workload_cluster_stacks = tenants.workload_cluster_stacks

        self.register_outputs(
            {
                "namespace": self.namespace,
                "service_account": self.service_account,
                "control_plane_stack": self.control_plane_stack,
                "workload_cluster_stacks": self.workload_cluster_stacks,
            }
        )
