"""Top-level PKO bootstrap component.

Composes the building blocks in this package into a single
ComponentResource the outer stack calls once:

    PKOBootstrap
    ├── PKORelease                   # Helm OCI install
    ├── StateBackend                 # PVC + passphrase Secret
    ├── WorkspaceServiceAccount      # pulumi-runner + ClusterRoleBindings
    └── Stack/ca4s-init              # runs env-specific init component

The init Stack CR reconciles from the same repo as this stack itself.
It is the only PKO Stack CR the outer host-side Pulumi program owns. Once PKO
runs it, the init stack reflexively instantiates control-plane and
tenants/workload resources from inside the management cluster.

The caller owns the PKO namespace and the Flux ``GitRepository`` source.
PKOBootstrap installs PKO and creates the init Stack CR that consumes that
source via ``spec.fluxSource`` instead of cloning Git directly.
"""

from __future__ import annotations

from typing import Any

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from pko._backend import StateBackend
from pko._release import PKO_NAMESPACE, PKORelease
from pko._service_account import WorkspaceServiceAccount
from pko._init_stack import INIT_PROJECT, INIT_REPO_DIR, init_stack_config
from pko._stack_cr import StackCRSpec, build_stack_spec


_WAIT_FOR_ANNOTATION = "pulumi.com/waitFor"
_WAIT_FOR_READY = "condition=Ready"
_INIT_STACK_TIMEOUT = "60m"


class PKOBootstrap(pulumi.ComponentResource):
    """Install PKO and hand off control to the inner Stack CRs.

    Args:
        name:
            Pulumi resource name. Used as a prefix for all children.
        provider:
            Kubernetes provider scoped to the management cluster.
        namespace_resource:
            Pre-created ``pulumi-kubernetes-operator`` Namespace resource shared
            by Flux source objects and PKO.
        flux_source_name:
            Name of the Flux ``GitRepository`` PKO Stack CRs should consume.
        flux_source_resource:
            Optional resource dependency for the concrete Flux source object;
            the init Stack CR waits for it before being created.
        config:
            Optional inline Pulumi config map to forward to child Stack CRs.
            PKOBootstrap passes this through the init Stack CR unchanged;
            project-scoped config keys determine which child stack consumes a
            value.
        env:
            Outer-stack environment moniker (``pulumi.get_stack()``).
            Propagated to the init Stack CR's ``spec.stack``; the init stack
            then uses the same value for the env-specific init and tenants
            component.
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
        init_stack:
            ``metadata.name`` of the single outer-owned init Stack CR.
    """

    namespace: Output[str]
    service_account: Output[str]
    flux_source_name: Output[str]
    init_stack: Output[str]

    def __init__(
        self,
        name: str,
        *,
        provider: k8s.Provider,
        namespace_resource: pulumi.Resource,
        flux_source_name: pulumi.Input[str],
        flux_source_resource: pulumi.Resource | None = None,
        env: str,
        config: dict[str, Any] | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:pko:PKOBootstrap", name, props={}, opts=opts)

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

        # Bundle the shared shape once. PKOBootstrap passes it as init-stack
        # config; the PKO-owned init stack reconstructs it and uses it for all
        # child Stack CRs.
        stack_spec = StackCRSpec(
            pko_namespace=release.namespace,
            service_account_name=sa.service_account_name,
            flux_source_name=flux_source_name,
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
            sa,
            backend,
        ]
        if flux_source_resource is not None:
            cr_deps.append(flux_source_resource)

        init_spec = build_stack_spec(
            spec=stack_spec,
            project_name=INIT_PROJECT,
            env=env,
            repo_dir=INIT_REPO_DIR,
            config=init_stack_config(
                stack_spec=stack_spec,
                child_config=config,
            ),
        )
        init_stack = k8s.apiextensions.CustomResource(
            f"{name}-init",
            api_version="pulumi.com/v1",
            kind="Stack",
            metadata={
                "namespace": PKO_NAMESPACE,
                "annotations": {_WAIT_FOR_ANNOTATION: _WAIT_FOR_READY},
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
        self.flux_source_name = Output.from_input(flux_source_name)
        self.init_stack = init_stack_name

        self.register_outputs(
            {
                "namespace": self.namespace,
                "service_account": self.service_account,
                "flux_source_name": self.flux_source_name,
                "init_stack": self.init_stack,
            }
        )
