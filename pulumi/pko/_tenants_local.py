"""Per-env concrete ``Tenants`` implementation for the ``local`` env.

Owns the single workload-cluster Stack CR for the ``local`` outer
stack. Selected at runtime by the dispatcher in
:class:`pko._tenants.Tenants` when ``pulumi.get_stack() == "local"``.

The local env is a single-tenant dev loop by design — a kind cluster
running one workload cluster is enough to validate the whole control
plane / Slurm path. Multi-tenant fan-out, if ever wanted locally,
graduates to its own ``_tenants_<env>.py`` (e.g. ``_tenants_localmulti``).
Cloud envs that need real fan-out get their own sibling module with
their own inventory shape (set, list-from-config, derived-from-CAPI,
etc.).
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from pko._stack_cr import StackCRSpec, build_stack_spec


# Outer-stack env moniker this concrete class is scoped to. Becomes
# the first segment of the workload-cluster Stack CR's stack name
# (``<env>-<tenant>``). Hard-coded because this module IS the local
# env's implementation.
_OUTER_ENV = "local"

# The single local tenant. Becomes the second segment of the Stack
# CR's stack name and must be DNS-label safe.
_TENANT = "example"

# Workload-cluster inner stack identity. Kebab-case Pulumi project
# name; must match the ``name:`` field in
# ``pulumi/stacks/workload_cluster/Pulumi.yaml``.
_WORKLOAD_CLUSTER_PROJECT = "ca4s-workload-cluster"
_WORKLOAD_CLUSTER_REPO_DIR = "pulumi/stacks/workload_cluster/"


class TenantsLocal(pulumi.ComponentResource):
    """Emit the single ``local`` workload-cluster Stack CR.

    Args:
        name: Pulumi resource name; prefix for the child Stack CR.
        stack_spec: Shared :class:`StackCRSpec` built once by
            :class:`pko.pko_bootstrap.PKOBootstrap` and threaded into
            the Stack CR this component emits.
        control_plane_stack: ``metadata.name`` of the control-plane
            Stack CR. The workload-cluster Stack CR sets it as a
            PKO-level ``spec.prerequisites`` entry so workload reconcile
            blocks until the control plane is reconciled.
        provider: Kubernetes provider scoped to the management cluster.
        opts: Standard ``ResourceOptions``.

    Outputs:
        workload_cluster_stacks: Singleton list with the emitted Stack
            CR's ``metadata.name``. The list shape is preserved (vs
            a bare Output) so the contract matches multi-tenant envs
            and consumers don't have to special-case ``local``.
    """

    workload_cluster_stacks: list[Output[str]]

    def __init__(
        self,
        name: str,
        *,
        stack_spec: StackCRSpec,
        control_plane_stack: pulumi.Input[str],
        provider: k8s.Provider,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:pko:TenantsLocal", name, props={}, opts=opts
        )

        # ``<_OUTER_ENV>-<_TENANT>`` becomes the third segment of the
        # Stack CR's ``spec.stack``. The workload-cluster dispatcher in
        # its workspace pod splits on the first ``-`` to recover the
        # env half and the tenant half.
        cr_spec = build_stack_spec(
            spec=stack_spec,
            project_name=_WORKLOAD_CLUSTER_PROJECT,
            env=f"{_OUTER_ENV}-{_TENANT}",
            repo_dir=_WORKLOAD_CLUSTER_REPO_DIR,
            prerequisites=[control_plane_stack],
        )
        # Per-env customization hook: any local-only Stack-CR spec
        # tweaks (extra ``config`` keys, alternate
        # ``resyncFrequencySeconds``, an overlay onto
        # ``workspaceTemplate``, ...) live HERE before the CR ships.
        # The shared :func:`build_stack_spec` only knows the cross-env
        # boilerplate; anything local-specific stays in this file so
        # other envs aren't carrying dead branches.
        cr = k8s.apiextensions.CustomResource(
            f"{name}-{_TENANT}",
            api_version="pulumi.com/v1",
            kind="Stack",
            metadata={"namespace": stack_spec.pko_namespace},
            spec=cr_spec,
            opts=ResourceOptions(parent=self, provider=provider),
        )

        self.workload_cluster_stacks = [cr.metadata.name]
        self.register_outputs(
            {"workload_cluster_stacks": self.workload_cluster_stacks}
        )
