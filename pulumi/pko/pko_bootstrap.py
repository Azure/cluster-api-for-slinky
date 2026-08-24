"""Top-level PKO bootstrap component.

Composes the building blocks in this package into a single
ComponentResource the outer stack calls once:

    PKOBootstrap
    ├── PKORelease                   # Helm OCI install
    ├── StateBackend                 # PVC + passphrase Secret
    ├── WorkspaceServiceAccount      # pulumi-runner + ClusterRoleBindings
    └── Stack/ca4s-init              # runs the unified init component

The init Stack CR reconciles from the same repo as this stack itself.
It is the only PKO Stack CR the outer host-side Pulumi program owns. Once PKO
runs it, the init stack reflexively instantiates control-plane and
tenants/workload resources from inside the management cluster.

PKOBootstrap owns the PKO namespace and creates the init Stack CR that consumes
the supplied Flux source via ``spec.fluxSource`` instead of cloning Git directly.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from fluxcd import FluxSource
from pko import PKO_NAMESPACE
from pko._backend import StateBackend
from pko._release import PKORelease
from pko._service_account import WorkspaceServiceAccount
from stacks.control_plane.control_plane_config import (
    ControlPlaneAWXConfig,
)
from stacks.init.init_stack import (
    INIT_PROJECT,
    INIT_REPO_DIR,
    INIT_STACK_CONFIG_KEY,
    InitStackConfig,
)
from stacks.kubernetes_annotations import (
    DELETE_PROPAGATION_ORPHAN,
    PULUMI_DELETION_PROPAGATION_POLICY_ANNOTATION,
    pulumi_wait_for,
)
from stacks.stack_cr import StackCRConfig, build_stack_spec


# Annotation + timeout for the single outer-owned init Stack CR.
# pulumi-kubernetes honors ``pulumi.com/waitFor`` on CRD writes by polling
# the named condition until it goes True; combined with the custom
# create/update timeout below, the outer ``pulumi up`` blocks until PKO
# reports the init stack Ready (which, for an env like ``azure``, means
# CAPI Operator + CAPZ + ASO are all installed and the AzureClusterIdentity
# CR has been applied). Without this gate, ``pulumi up`` returns the
# moment the Stack CR is submitted and operators must poll for the init
# stack themselves — brittle for CI and for our test runbook.
_WAIT_FOR_READY = "condition=Ready"
# The enclosing Stack delete must outlast the inner workload delete timeout
# plus PKO retry and finalization overhead.
_INIT_STACK_TIMEOUT = "150m"


def _init_stack_config_with_flux_source(
    *,
    init_stack_config: pulumi.Input[InitStackConfig] | None,
    flux_source_name: pulumi.Input[str],
    flux_source_namespace: pulumi.Input[str],
) -> pulumi.Input[InitStackConfig]:
    def merge_flux_source(resolved: dict[str, object]) -> InitStackConfig:
        base_config_value = resolved["base_config"]
        base_config = (
            base_config_value
            if isinstance(base_config_value, InitStackConfig)
            else InitStackConfig.model_validate(base_config_value)
        )
        base_awx_config = base_config.control_plane.deployments.awx
        if not base_awx_config.enabled:
            return base_config

        control_plane = base_config.control_plane
        deployments = control_plane.deployments
        awx_config = ControlPlaneAWXConfig(
            **{
                **base_awx_config.model_dump(),
                "flux_source_name": resolved["flux_source_name"],
                "flux_source_namespace": resolved["flux_source_namespace"],
            }
        )
        return base_config.model_copy(
            update={
                "control_plane": control_plane.model_copy(
                    update={
                        "deployments": deployments.model_copy(
                            update={"awx": awx_config}
                        )
                    }
                )
            }
        )

    return pulumi.Output.all(
        base_config=init_stack_config or InitStackConfig(),
        flux_source_name=flux_source_name,
        flux_source_namespace=flux_source_namespace,
    ).apply(merge_flux_source)


def _init_stack_config_to_config(
    init_stack_config: pulumi.Input[InitStackConfig],
) -> pulumi.Output[dict[str, object]]:
    def unresolved_config(config: InitStackConfig) -> dict[str, object]:
        return config.model_dump(
            by_alias=False,
            mode="python",
            exclude_none=True,
            exclude_defaults=True,
        )

    return pulumi.Output.from_input(init_stack_config).apply(
        lambda config: pulumi.Output.from_input(unresolved_config(config))
    ).apply(
        lambda config: InitStackConfig.model_validate(config).to_config()
    )


class PKOBootstrap(pulumi.ComponentResource):
    """Install PKO and hand off control to the inner Stack CRs.

    Args:
        name:
            Pulumi resource name. Used as a prefix for all children.
        provider:
            Kubernetes provider scoped to the management cluster.
        flux_source:
            Flux source handle PKO Stack CRs should consume.
        config:
            Optional inline Pulumi config map to forward to child Stack CRs.
            PKOBootstrap passes this through the init Stack CR unchanged;
            project-scoped config keys determine which child stack consumes a
            value.
        env:
            Outer-stack environment moniker (``pulumi.get_stack()``).
            Propagated to the init Stack CR's ``spec.stack``; the init stack
            uses the same value to select control-plane and tenant arguments.
        opts:
            Standard Pulumi ``ResourceOptions``.

    The init Stack CR is annotated with ``pulumi.com/waitFor`` so the outer
    ``pulumi up`` does not return until PKO reports the init stack Ready.

    Outputs:
        namespace:
            PKO namespace name (constant ``pulumi-kubernetes-operator``).
        service_account:
            Workspace SA name (constant ``pulumi-runner``).
        flux_source_name:
            Shared Flux ``GitRepository`` source consumed by PKO Stack CRs.
        flux_source_namespace:
            Namespace containing the Flux source consumed by PKO Stack CRs.
        init_stack:
            ``metadata.name`` of the single outer-owned init Stack CR.
    """

    namespace: Output[str]
    service_account: Output[str]
    flux_source_name: Output[str]
    flux_source_namespace: Output[str]
    init_stack: Output[str]

    def __init__(
        self,
        name: str,
        *,
        provider: k8s.Provider,
        namespace_resource: pulumi.Resource | None = None,
        flux_source: FluxSource,
        env: str,
        init_stack_config: pulumi.Input[InitStackConfig] | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:pko:PKOBootstrap", name, props={}, opts=opts)

        if namespace_resource is None:
            namespace_resource = k8s.core.v1.Namespace(
                f"{name}-ns",
                metadata={"name": PKO_NAMESPACE},
                opts=ResourceOptions(parent=self, provider=provider),
            )

        release = PKORelease(
            f"{name}-release",
            provider=provider,
            namespace_resource=namespace_resource,
            opts=ResourceOptions(parent=self),
        )

        backend = StateBackend(
            f"{name}-backend",
            namespace=release.namespace,
            provider=provider,
            opts=ResourceOptions(parent=self),
        )

        sa = WorkspaceServiceAccount(
            f"{name}-sa",
            namespace=release.namespace,
            provider=provider,
            opts=ResourceOptions(parent=self),
        )

        # Bundle the shared shape used to build the outer-owned init Stack CR.
        stack_spec = pulumi.Output.all(
            pko_namespace=release.namespace,
            service_account_name=sa.service_account_name,
            flux_source_name=flux_source.source_name,
            flux_source_namespace=flux_source.namespace,
            flux_source_api_version=flux_source.api_version,
            flux_source_kind=flux_source.kind,
            state_pvc_name=backend.pvc_name,
            state_backend_url=backend.backend_url,
            passphrase_secret_name=backend.passphrase_secret_name,
        ).apply(StackCRConfig.model_validate)

        # Everything must wait for PKO itself to be Ready. ``depends_on``
        # the release's status Output is enough — pulumi-kubernetes
        # blocks the Release on its readiness before emitting the
        # ``status`` Output.
        cr_deps: list[pulumi.Resource] = [
            namespace_resource,
            release,
            sa,
            backend,
            flux_source.resource,
        ]

        init_spec = build_stack_spec(
            spec=stack_spec,
            project_name=INIT_PROJECT,
            env=env,
            repo_dir=INIT_REPO_DIR,
            config={
                INIT_STACK_CONFIG_KEY: _init_stack_config_to_config(
                    init_stack_config=_init_stack_config_with_flux_source(
                        init_stack_config=init_stack_config,
                        flux_source_name=flux_source.source_name,
                        flux_source_namespace=flux_source.namespace,
                    )
                )
            },
        )
        init_stack = k8s.apiextensions.CustomResource(
            f"{name}-init",
            api_version="pulumi.com/v1",
            kind="Stack",
            metadata={
                "name": f"{name}-init",
                "namespace": PKO_NAMESPACE,
                "annotations": {
                    **pulumi_wait_for(_WAIT_FOR_READY),
                    # PKO's Stack finalizer runs ``pulumi destroy`` through the
                    # generated Workspace pod. Foreground cascading deletion can
                    # delete that Workspace/pod first, interrupting destroy with
                    # "^C received; cancelling" and leaving the Stack stuck on
                    # finalizers (pulumi-kubernetes-operator/#1181).
                    # Orphan propagation keeps PKO's Workspace alive long enough
                    # for ``destroyOnFinalize`` to finish its own cleanup path.
                    PULUMI_DELETION_PROPAGATION_POLICY_ANNOTATION: (
                        DELETE_PROPAGATION_ORPHAN
                    ),
                },
            },
            spec=init_spec,
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=cr_deps,
                custom_timeouts=pulumi.CustomTimeouts(
                    create=_INIT_STACK_TIMEOUT,
                    update=_INIT_STACK_TIMEOUT,
                    delete=_INIT_STACK_TIMEOUT,
                ),
            ),
        )
        init_stack_name = init_stack.metadata["name"]  # type: ignore[attr-defined]

        self.namespace = release.namespace
        self.service_account = sa.service_account_name
        self.flux_source_name = flux_source.source_name
        self.flux_source_namespace = flux_source.namespace
        self.init_stack = init_stack_name

        self.register_outputs(
            {
                "namespace": self.namespace,
                "service_account": self.service_account,
                "flux_source_name": self.flux_source_name,
                "flux_source_namespace": self.flux_source_namespace,
                "init_stack": self.init_stack,
            }
        )
