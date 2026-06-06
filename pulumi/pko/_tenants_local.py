"""Per-env concrete ``Tenants`` implementation for the ``local`` env.

Infers the workload-cluster Stack CR inventory for the ``local`` outer
stack from ``workload_cluster_local_*.py`` tenant modules. Selected at
runtime by the dispatcher in
:class:`pko._tenants.Tenants` when ``pulumi.get_stack() == "local"``.

The local env usually runs as a single-tenant dev loop — a kind cluster
running one workload cluster is enough to validate the whole control
plane / Slurm path. Multi-tenant fan-out locally is just more
``workload_cluster_local_<tenant>.py`` modules.
Cloud envs that need real fan-out get their own sibling module with
their own inventory shape (set, list-from-config, derived-from-CAPI,
etc.).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from pko._stack_cr import StackCRSpec, build_stack_spec


# Outer-stack env moniker this concrete class is scoped to. Becomes
# the first segment of the workload-cluster Stack CR's stack name
# (``<env>-<tenant>``). Hard-coded because this module IS the local
# env's implementation.
_OUTER_ENV = "local"

# Workload-cluster inner stack identity. Kebab-case Pulumi project
# name; must match the ``name:`` field in
# ``pulumi/stacks/workload_cluster/Pulumi.yaml``.
_WORKLOAD_CLUSTER_PROJECT = "ca4s-workload-cluster"
_WORKLOAD_CLUSTER_REPO_DIR = "pulumi/stacks/workload_cluster/"
_WORKLOAD_CLUSTER_DIR = (
    Path(__file__).resolve().parents[1] / "stacks" / "workload_cluster"
)
_TENANT_MODULE_PREFIX = "workload_cluster_local_"
_TENANT_MODULE_GLOB = f"{_TENANT_MODULE_PREFIX}*.py"
_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")


def _tenant_from_module(path: Path) -> str:
    suffix = path.stem.removeprefix(_TENANT_MODULE_PREFIX)
    numeric_tenant_suffix = suffix.removeprefix("tenant_")
    if suffix.startswith("tenant_") and numeric_tenant_suffix[:1].isdigit():
        suffix = numeric_tenant_suffix
    tenant = suffix.replace("_", "-")
    if not tenant or len(tenant) > 63 or not _DNS_LABEL.fullmatch(tenant):
        raise ValueError(
            f"local workload tenant module {path.name!r} infers invalid "
            f"DNS-label tenant name {tenant!r}"
        )
    return tenant


def _local_tenants() -> tuple[str, ...]:
    return tuple(
        sorted(
            _tenant_from_module(path)
            for path in _WORKLOAD_CLUSTER_DIR.glob(_TENANT_MODULE_GLOB)
        )
    )


class TenantsLocal(pulumi.ComponentResource):
    """Emit ``local`` workload-cluster Stack CRs.

    Args:
        name: Pulumi resource name; prefix for the child Stack CR.
        stack_spec: Shared :class:`StackCRSpec` built once by
            :class:`pko.pko_bootstrap.PKOBootstrap` and threaded into
            the Stack CR this component emits.
        control_plane_stack: ``metadata.name`` of the control-plane
            Stack CR. The workload-cluster Stack CR sets it as a
            PKO-level ``spec.prerequisites`` entry so workload reconcile
            blocks until the control plane is reconciled.
        config: Optional inline Pulumi config map written into emitted Stack
            CRs. Opaque pass-through; the inner project owns key semantics.
        provider: Kubernetes provider scoped to the management cluster.
        opts: Standard ``ResourceOptions``.

    Outputs:
        workload_cluster_stacks: List of emitted Stack CR ``metadata.name``
            values, one per discovered ``workload_cluster_local_*.py`` module.
            The list shape is preserved so consumers don't have to special-case
            ``local``.
    """

    workload_cluster_stacks: list[Output[str]]

    def __init__(
        self,
        name: str,
        *,
        stack_spec: StackCRSpec,
        control_plane_stack: pulumi.Input[str],
        config: dict[str, Any] | None,
        provider: k8s.Provider,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:pko:TenantsLocal", name, props={}, opts=opts
        )

        workload_cluster_stacks: list[Output[str]] = []
        for tenant in _local_tenants():
            # ``<_OUTER_ENV>-<tenant>`` becomes the third segment of the
            # Stack CR's ``spec.stack``. The workload-cluster dispatcher in
            # its workspace pod splits on the first ``-`` to recover the
            # env half and the tenant half.
            cr_spec = build_stack_spec(
                spec=stack_spec,
                project_name=_WORKLOAD_CLUSTER_PROJECT,
                env=f"{_OUTER_ENV}-{tenant}",
                repo_dir=_WORKLOAD_CLUSTER_REPO_DIR,
                config=config,
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
                f"{name}-{tenant}",
                api_version="pulumi.com/v1",
                kind="Stack",
                metadata={"namespace": stack_spec.pko_namespace},
                spec=cr_spec,
                opts=ResourceOptions(parent=self, provider=provider),
            )
            workload_cluster_stacks.append(cr.metadata["name"])  # type: ignore[attr-defined]

        self.workload_cluster_stacks = workload_cluster_stacks
        self.register_outputs(
            {"workload_cluster_stacks": self.workload_cluster_stacks}
        )
