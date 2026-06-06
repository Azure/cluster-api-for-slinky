"""Top-level PKO bootstrap component.

Composes the building blocks in this package into a single
ComponentResource the outer stack calls once:

    PKOBootstrap
    ├── Namespace/pulumi-kubernetes-operator
    ├── Secret/gitea-ssh-<hash>             # admin SSH private key
    ├── ConfigMap/gitea-known-hosts-<hash>  # SSH host-key trust
    ├── PKORelease                   # Helm OCI install w/ known_hosts mount
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
Gitea via the REST API, the private user-key lands in a public-key-hash
named source Secret, and the known_hosts entry derived from the host pubkey
lands in a content-hash named ConfigMap. PKOBootstrap copies the private key
Secret into PKO's namespace for ``Stack.spec.gitAuth``;
:mod:`pko._release` and :mod:`pko._stack_cr` mount only the public host-key
trust file.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

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

# Names PKOBootstrap projects into PKO's namespace. The Secret name is supplied
# by the GitOpsRepository because it is derived from the matching public key;
# the ConfigMap name is derived here from the known_hosts content. Both names
# change only when their underlying key material changes.
_SSH_PRIVATE_KEY_KEY = "id_ed25519"
_SSH_KNOWN_HOSTS_CONFIG_MAP_PREFIX = "gitea-known-hosts"
_SSH_KNOWN_HOSTS_KEY = "known_hosts"


def _name_with_content_hash(prefix: str, content: str) -> str:
    """Build a Kubernetes-safe name that changes only when content changes."""
    normalized = content.strip()
    if not normalized:
        raise ValueError(f"cannot build {prefix!r} name from empty content")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _copy_secret_data_key(
    data: Mapping[str, str] | None,
    source_key: str,
) -> str:
    if not data or source_key not in data:
        available = sorted(data.keys()) if data else []
        raise ValueError(
            f"source SSH Secret is missing key {source_key!r}; available keys: {available!r}"
        )
    return data[source_key]


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
        ssh_private_key_secret:
            Source Kubernetes Secret resource containing the PKCS8 PEM admin
            private key. PKOBootstrap copies from this Secret into the PKO
            namespace.
        ssh_known_hosts:
            Output[str] single-line ``known_hosts`` entry for the SSH
            endpoint (``GitOpsRepository.ssh_known_hosts``). Projected
            into a hash-named ConfigMap under key
            ``known_hosts``.
        config:
            Optional inline Pulumi config map written into every inner Stack CR.
            PKOBootstrap treats this as opaque pass-through; project-scoped
            config keys determine which inner stack actually consumes a value.
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
    ssh_secret_name: Output[str]
    known_hosts_config_map_name: Output[str]
    control_plane_stack: Output[str]
    workload_cluster_stacks: list[Output[str]]

    def __init__(
        self,
        name: str,
        *,
        kubeconfig: pulumi.Input[str],
        repo_url: pulumi.Input[str],
        repo_branch: pulumi.Input[str],
        ssh_private_key_secret: k8s.core.v1.Secret,
        ssh_known_hosts: pulumi.Input[str],
        env: str,
        config: dict[str, Any] | None = None,
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

        # Namespace owned here (not in PKORelease) so the known_hosts
        # ConfigMap can be created in the same ns BEFORE the Helm install
        # references it. The chart install is ``atomic=True``, so a missing
        # ConfigMap volume at install time would cause a rollback.
        ns = k8s.core.v1.Namespace(
            f"{name}-ns",
            metadata={"name": PKO_NAMESPACE},
            opts=ResourceOptions(parent=self, provider=k8s_provider),
        )

        ssh_secret_name = Output.unsecret(
            Output.from_input(ssh_private_key_secret.metadata["name"])
        )
        known_hosts_content = Output.from_input(ssh_known_hosts)
        known_hosts_config_map_name = known_hosts_content.apply(
            lambda content: _name_with_content_hash(
                _SSH_KNOWN_HOSTS_CONFIG_MAP_PREFIX, content
            )
        )

        ssh_private_key_data = Output.secret(
            ssh_private_key_secret.data.apply(
                lambda data: _copy_secret_data_key(data, _SSH_PRIVATE_KEY_KEY)
            )
        )

        # The Secret PKO reads admin SSH credentials from. The private
        # key is referenced by Stack CR ``gitAuth.sshAuth.sshPrivateKey``;
        # it is not mounted into operator or workspace pods as a file.
        ssh_secret = k8s.core.v1.Secret(
            f"{name}-ssh",
            metadata={
                "name": ssh_secret_name,
                "namespace": PKO_NAMESPACE,
            },
            type="Opaque",
            immutable=True,
            data={
                _SSH_PRIVATE_KEY_KEY: ssh_private_key_data,
            },
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[ns, ssh_private_key_secret],
            ),
        )

        known_hosts = k8s.core.v1.ConfigMap(
            f"{name}-known-hosts",
            metadata={
                "name": known_hosts_config_map_name,
                "namespace": PKO_NAMESPACE,
            },
            data={_SSH_KNOWN_HOSTS_KEY: ssh_known_hosts},
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
            known_hosts_config_map_name=known_hosts_config_map_name,
            known_hosts_resource=known_hosts,
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
            ssh_secret_name=ssh_secret_name,
            known_hosts_config_map_name=known_hosts_config_map_name,
            state_pvc_name=backend.pvc_name,
            state_backend_url=backend.backend_url,
            passphrase_secret_name=backend.passphrase_secret_name,
        )

        # Everything must wait for PKO itself to be Ready. ``depends_on``
        # the release's status Output is enough — pulumi-kubernetes
        # blocks the Release on its readiness before emitting the
        # ``status`` Output.
        cr_deps: list[pulumi.Resource] = [
            release,
            ssh_secret,
            known_hosts,
            sa,
            backend,
        ]

        control_plane_spec = build_stack_spec(
            spec=stack_spec,
            project_name=CONTROL_PLANE_PROJECT,
            env=env,
            repo_dir=CONTROL_PLANE_REPO_DIR,
            config=config,
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
        control_plane_stack_name = control_plane.metadata["name"]  # type: ignore[attr-defined]

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
            control_plane_stack=control_plane_stack_name,
            config=config,
            provider=k8s_provider,
            opts=ResourceOptions(
                parent=self, depends_on=cr_deps + [control_plane]
            ),
        )

        self.namespace = release.namespace
        self.service_account = sa.service_account_name
        self.ssh_secret_name = ssh_secret_name
        self.known_hosts_config_map_name = known_hosts_config_map_name
        self.control_plane_stack = control_plane_stack_name
        self.workload_cluster_stacks = tenants.workload_cluster_stacks

        self.register_outputs(
            {
                "namespace": self.namespace,
                "service_account": self.service_account,
                "ssh_secret_name": self.ssh_secret_name,
                "known_hosts_config_map_name": self.known_hosts_config_map_name,
                "control_plane_stack": self.control_plane_stack,
                "workload_cluster_stacks": self.workload_cluster_stacks,
            }
        )
