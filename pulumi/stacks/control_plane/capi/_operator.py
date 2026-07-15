"""Cluster API Operator and provider installation.

Owns the tenant-agnostic CAPI management-plane layer:

* ``cluster-api-operator`` Helm release, pinned to
  :data:`CAPI_OPERATOR_CHART_VERSION`.
* Provider namespaces for core, kubeadm bootstrap, kubeadm control plane,
  and Docker infrastructure.
* Provider CRs reconciled by the operator into the actual CAPI provider
  controllers and CRDs.

This is the Pulumi equivalent of the provider-install portion of
``clusterctl init --infrastructure docker``. It deliberately stops before
declaring any workload ``Cluster`` / ``ClusterClass`` objects; those are
tenant-scoped and belong in tenants/workload components once the providers are
present.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from stacks.control_plane.control_plane_config import (
    DockerInfrastructureProviderConfig,
    InfrastructureProviderConfig,
    InfrastructureProvidersConfig,
)
from stacks.kubernetes_annotations import (
    pulumi_wait_for,
)


CAPI_OPERATOR_CHART_REPO = "https://kubernetes-sigs.github.io/cluster-api-operator"
CAPI_OPERATOR_CHART_NAME = "cluster-api-operator"
CAPI_OPERATOR_CHART_VERSION = "0.27.0"
CAPI_OPERATOR_RELEASE_NAME = "cluster-api-operator"

# Keep this aligned with the CAPI v1beta2 workload resources emitted by the
# local workload-cluster class.
CAPI_PROVIDER_VERSION = "v1.13.2"

# CAPZ release paired with the CAPI provider pin above.
# https://github.com/kubernetes-sigs/cluster-api-provider-azure/releases/tag/v1.24.1
# When CAPI_PROVIDER_VERSION moves, look for the next CAPZ release whose notes
# call out a matching CAPI bump and update both together. CAPZ auto-installs the
# Azure Service Operator (ASO) into the same ``capz-system`` namespace since
# CAPZ v1.11.0 — there is no separate ASO provider CR.
CAPZ_PROVIDER_VERSION = "v1.24.1"

CAPI_OPERATOR_NAMESPACE = "capi-operator-system"
CAPI_CORE_NAMESPACE = "capi-system"
CAPI_BOOTSTRAP_NAMESPACE = "kubeadm-bootstrap-system"
CAPI_CONTROL_PLANE_NAMESPACE = "kubeadm-control-plane-system"
CAPI_DOCKER_INFRASTRUCTURE_NAMESPACE = "docker-infrastructure-system"
# CAPZ controller + AzureCluster/AzureManaged* CRDs + ASO controller all share
# this namespace by upstream convention. The CAPI Operator follows the same
# placement when it reconciles the azure InfrastructureProvider CR.
CAPI_AZURE_INFRASTRUCTURE_NAMESPACE = "capz-system"

_PROVIDER_API_VERSION = "operator.cluster.x-k8s.io/v1alpha2"
_PROVIDER_FEATURE_GATES = {
    "ClusterTopology": True,
}
_PROVIDER_MANAGER_CONTAINER = "manager"
_WAIT_FOR_READY = "condition=Ready"
_WAIT_FOR_AVAILABLE = "condition=Available"
_WAIT_FOR_WEBHOOK_CA_BUNDLE = "jsonpath={.webhooks[*].clientConfig.caBundle}"

_WebhookConfigurationPatch = (
    k8s.admissionregistration.v1.MutatingWebhookConfigurationPatch
    | k8s.admissionregistration.v1.ValidatingWebhookConfigurationPatch
)

_CAPI_OPERATOR_WEBHOOK_CONFIGURATIONS = {
    "mutating": "capi-operator-mutating-webhook-configuration",
    "validating": "capi-operator-validating-webhook-configuration",
}

# Per-core-provider webhook configuration names. Infrastructure providers carry
# their own webhook names in :data:`_INFRASTRUCTURE_PROVIDERS` because the same
# role can host multiple concrete providers (docker, azure, ...).
_PROVIDER_WEBHOOK_CONFIGURATIONS = {
    "core": {
        "mutating": "capi-mutating-webhook-configuration",
        "validating": "capi-validating-webhook-configuration",
    },
    "bootstrap": {
        "mutating": "capi-kubeadm-bootstrap-mutating-webhook-configuration",
        "validating": "capi-kubeadm-bootstrap-validating-webhook-configuration",
    },
    "control_plane": {
        "mutating": "capi-kubeadm-control-plane-mutating-webhook-configuration",
        "validating": "capi-kubeadm-control-plane-validating-webhook-configuration",
    },
}

_PROVIDER_CONTROLLER_DEPLOYMENTS = {
    "core": "capi-controller-manager",
    "bootstrap": "capi-kubeadm-bootstrap-controller-manager",
    "control_plane": "capi-kubeadm-control-plane-controller-manager",
}

# Per-infrastructure-provider metadata. Keyed by the ``InfrastructureProvider``
# ``spec.name`` (which is also the upstream provider name the CAPI Operator
# uses to resolve the right release artifacts). Each entry pins:
#
#   * ``namespace`` — where the CAPI Operator drops the provider's controller
#     Deployment + CRDs.
#   * ``version``   — the provider release tag to pin. Distinct from
#     ``CAPI_PROVIDER_VERSION`` because each infrastructure provider has its
#     own release cadence; the pairing rules are documented next to the
#     version constant for each provider.
#   * ``deployment`` — provider controller Deployment name; this must
#     be Available before dependents create CRs that hit its webhook.
#   * ``webhooks``  — the names of the mutating/validating webhook
#     configurations the provider's controller installs, so we can attach the
#     usual ``waitFor=caBundle`` patch and gate dependents on webhook
#     readiness.
_INFRASTRUCTURE_PROVIDERS: dict[str, dict[str, object]] = {
    "docker": {
        "namespace": CAPI_DOCKER_INFRASTRUCTURE_NAMESPACE,
        # CAPD ships within the CAPI repo, so it shares the CAPI pin.
        "version": CAPI_PROVIDER_VERSION,
        "deployment": "capd-controller-manager",
        "webhooks": {
            "mutating": "capd-mutating-webhook-configuration",
            "validating": "capd-validating-webhook-configuration",
        },
    },
    "azure": {
        "namespace": CAPI_AZURE_INFRASTRUCTURE_NAMESPACE,
        "version": CAPZ_PROVIDER_VERSION,
        "deployment": "capz-controller-manager",
        "webhooks": {
            "mutating": "capz-mutating-webhook-configuration",
            "validating": "capz-validating-webhook-configuration",
        },
    },
}


def _provider_spec(
    *,
    version: str,
    infrastructure_config: InfrastructureProviderConfig | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "version": version,
        "manager": {
            "featureGates": _PROVIDER_FEATURE_GATES,
        },
    }
    if infrastructure_config is None:
        return spec
    if infrastructure_config.provider_oci is not None:
        spec["fetchConfig"] = {"oci": infrastructure_config.provider_oci}
    if infrastructure_config.controller_image is not None:
        spec["deployment"] = {
            "containers": [
                {
                    "name": _PROVIDER_MANAGER_CONTAINER,
                    "imageUrl": infrastructure_config.controller_image,
                }
            ]
        }
    return spec


class ClusterAPIOperator(pulumi.ComponentResource):
    """Install Cluster API Operator and the requested CAPI providers.

    Args:
            * ``infrastructure_providers`` — complete infrastructure provider
                configuration. Enabled providers are installed; provider-specific OCI
                and image settings are rendered directly into their provider CRs.

    Outputs:
      * ``namespace`` — namespace containing the operator deployment.
      * ``release_status`` — Helm release status.
      * ``provider_version`` — pinned CAPI provider version (the CAPI version
        itself; infrastructure providers carry their own independently-pinned
        versions in :data:`_INFRASTRUCTURE_PROVIDERS`).
      * ``provider_namespaces`` — namespaces containing provider CRs and
        reconciled controllers. Keys: ``core``, ``bootstrap``,
        ``control_plane``, and one ``infrastructure_<name>`` per requested
        infrastructure provider (e.g. ``infrastructure_docker``,
        ``infrastructure_azure``).
    """

    namespace: Output[str]
    release_status: Output[object]
    provider_version: Output[str]
    provider_namespaces: dict[str, Output[str]]

    def __init__(
        self,
        name: str,
        *,
        cert_manager: pulumi.Resource | None = None,
        infrastructure_providers: InfrastructureProvidersConfig = (
            InfrastructureProvidersConfig(
                docker=DockerInfrastructureProviderConfig(enabled=True)
            )
        ),
        provider: k8s.Provider | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:control_plane:ClusterAPIOperator", name, props={}, opts=opts
        )

        operator_ns = k8s.core.v1.Namespace(
            f"{name}-operator-ns",
            metadata={"name": CAPI_OPERATOR_NAMESPACE},
            opts=ResourceOptions(parent=self, provider=provider),
        )

        release = k8s.helm.v3.Release(
            f"{name}-helm",
            name=CAPI_OPERATOR_RELEASE_NAME,
            chart=CAPI_OPERATOR_CHART_NAME,
            version=CAPI_OPERATOR_CHART_VERSION,
            repository_opts={"repo": CAPI_OPERATOR_CHART_REPO},
            namespace=CAPI_OPERATOR_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=600,
            values={},
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[operator_ns]
                + ([cert_manager] if cert_manager is not None else []),
            ),
        )
        operator_webhooks = self._webhook_configuration_patches(
            name,
            resource_name="operator",
            names=_CAPI_OPERATOR_WEBHOOK_CONFIGURATIONS,
            dependency=release,
            provider=provider,
        )

        # Validate the requested infrastructure providers up front so a typo
        # surfaces as a clear Python error at plan time, not a confusing CAPI
        # Operator reconciliation failure ten minutes into an apply.
        provider_configs = infrastructure_providers.enabled_providers()
        if not provider_configs:
            raise ValueError(
                "at least one CAPI infrastructure provider is required; pass "
                "an InfrastructureProvidersConfig with docker or azure enabled"
            )
        unknown = [
            name for name in provider_configs if name not in _INFRASTRUCTURE_PROVIDERS
        ]
        if unknown:
            known = sorted(_INFRASTRUCTURE_PROVIDERS.keys())
            raise ValueError(
                f"unknown CAPI infrastructure provider(s) {unknown!r}; "
                f"known providers: {known!r}"
            )

        core_ns = self._provider_namespace(name, "core", CAPI_CORE_NAMESPACE, provider)
        bootstrap_ns = self._provider_namespace(
            name, "bootstrap", CAPI_BOOTSTRAP_NAMESPACE, provider
        )
        control_plane_ns = self._provider_namespace(
            name, "control-plane", CAPI_CONTROL_PLANE_NAMESPACE, provider
        )
        # Per-infrastructure-provider namespaces created up front so we can
        # depend on them when constructing each InfrastructureProvider CR.
        infrastructure_namespaces: dict[str, k8s.core.v1.Namespace] = {
            infra_name: self._provider_namespace(
                name,
                f"{infra_name}-infra",
                str(_INFRASTRUCTURE_PROVIDERS[infra_name]["namespace"]),
                provider,
            )
            for infra_name in provider_configs
        }

        core_provider = self._provider_cr(
            name,
            resource_name="core",
            kind="CoreProvider",
            provider_name="cluster-api",
            namespace=CAPI_CORE_NAMESPACE,
            namespace_resource=core_ns,
            dependencies=[release, *operator_webhooks],
            provider=provider,
        )
        bootstrap_provider = self._provider_cr(
            name,
            resource_name="bootstrap-kubeadm",
            kind="BootstrapProvider",
            provider_name="kubeadm",
            namespace=CAPI_BOOTSTRAP_NAMESPACE,
            namespace_resource=bootstrap_ns,
            dependencies=[release, *operator_webhooks],
            provider=provider,
        )
        control_plane_provider = self._provider_cr(
            name,
            resource_name="control-plane-kubeadm",
            kind="ControlPlaneProvider",
            provider_name="kubeadm",
            namespace=CAPI_CONTROL_PLANE_NAMESPACE,
            namespace_resource=control_plane_ns,
            dependencies=[release, *operator_webhooks],
            provider=provider,
        )
        # Reconcile one InfrastructureProvider CR per requested infra. CR
        # ``metadata.name`` derives from the provider name (not Pulumi's
        # auto-name suffix) so the CR is stable across runs.
        infrastructure_provider_crs: dict[str, k8s.apiextensions.CustomResource] = {
            infra_name: self._provider_cr(
                name,
                resource_name=f"infrastructure-{infra_name}",
                kind="InfrastructureProvider",
                provider_name=infra_name,
                version=str(_INFRASTRUCTURE_PROVIDERS[infra_name]["version"]),
                namespace=str(_INFRASTRUCTURE_PROVIDERS[infra_name]["namespace"]),
                namespace_resource=infrastructure_namespaces[infra_name],
                dependencies=[release, *operator_webhooks],
                infrastructure_config=provider_config,
                provider=provider,
            )
            for infra_name, provider_config in provider_configs.items()
        }
        core_deployment_ready = self._provider_deployment_ready_patch(
            name,
            resource_name="core",
            deployment_name=_PROVIDER_CONTROLLER_DEPLOYMENTS["core"],
            namespace=CAPI_CORE_NAMESPACE,
            dependency=core_provider,
            provider=provider,
        )
        bootstrap_deployment_ready = self._provider_deployment_ready_patch(
            name,
            resource_name="bootstrap",
            deployment_name=_PROVIDER_CONTROLLER_DEPLOYMENTS["bootstrap"],
            namespace=CAPI_BOOTSTRAP_NAMESPACE,
            dependency=bootstrap_provider,
            provider=provider,
        )
        control_plane_deployment_ready = self._provider_deployment_ready_patch(
            name,
            resource_name="control-plane",
            deployment_name=_PROVIDER_CONTROLLER_DEPLOYMENTS["control_plane"],
            namespace=CAPI_CONTROL_PLANE_NAMESPACE,
            dependency=control_plane_provider,
            provider=provider,
        )
        infrastructure_deployment_ready: dict[str, k8s.apps.v1.DeploymentPatch] = {
            infra_name: self._provider_deployment_ready_patch(
                name,
                resource_name=f"infrastructure-{infra_name}",
                deployment_name=str(
                    _INFRASTRUCTURE_PROVIDERS[infra_name]["deployment"]
                ),
                namespace=str(_INFRASTRUCTURE_PROVIDERS[infra_name]["namespace"]),
                dependency=cr,
                provider=provider,
            )
            for infra_name, cr in infrastructure_provider_crs.items()
        }
        core_webhooks = self._webhook_configuration_patches(
            name,
            resource_name="core",
            names=_PROVIDER_WEBHOOK_CONFIGURATIONS["core"],
            dependency=core_deployment_ready,
            provider=provider,
        )
        bootstrap_webhooks = self._webhook_configuration_patches(
            name,
            resource_name="bootstrap",
            names=_PROVIDER_WEBHOOK_CONFIGURATIONS["bootstrap"],
            dependency=bootstrap_deployment_ready,
            provider=provider,
        )
        control_plane_webhooks = self._webhook_configuration_patches(
            name,
            resource_name="control-plane",
            names=_PROVIDER_WEBHOOK_CONFIGURATIONS["control_plane"],
            dependency=control_plane_deployment_ready,
            provider=provider,
        )
        infrastructure_webhook_patches: dict[
            str, list[_WebhookConfigurationPatch]
        ] = {
            infra_name: self._webhook_configuration_patches(
                name,
                resource_name=f"infrastructure-{infra_name}",
                names=_INFRASTRUCTURE_PROVIDERS[infra_name]["webhooks"],  # type: ignore[arg-type]
                dependency=infrastructure_deployment_ready[infra_name],
                provider=provider,
            )
            for infra_name, cr in infrastructure_provider_crs.items()
        }
        webhook_patches: dict[str, list[_WebhookConfigurationPatch]] = {
            "operator": operator_webhooks,
            "core": core_webhooks,
            "bootstrap": bootstrap_webhooks,
            "control_plane": control_plane_webhooks,
        }
        for infra_name, patches in infrastructure_webhook_patches.items():
            webhook_patches[f"infrastructure_{infra_name}"] = patches

        self.namespace = Output.from_input(CAPI_OPERATOR_NAMESPACE)
        self.release_status = release.status
        self.provider_version = Output.from_input(CAPI_PROVIDER_VERSION)
        # Core providers always live at the same key; infrastructure providers
        # get a per-name key so multiple infras coexist cleanly in the same
        # dict. See the ClusterAPIOperator docstring for the exact key set.
        self.provider_namespaces = {
            "core": Output.from_input(CAPI_CORE_NAMESPACE),
            "bootstrap": Output.from_input(CAPI_BOOTSTRAP_NAMESPACE),
            "control_plane": Output.from_input(CAPI_CONTROL_PLANE_NAMESPACE),
        }
        for infra_name in provider_configs:
            self.provider_namespaces[f"infrastructure_{infra_name}"] = (
                Output.from_input(
                    str(_INFRASTRUCTURE_PROVIDERS[infra_name]["namespace"])
                )
            )

        providers_output: dict[str, Output[str]] = {
            "core": core_provider.metadata["name"],  # type: ignore[assignment]
            "bootstrap": bootstrap_provider.metadata["name"],  # type: ignore[assignment]
            "control_plane": control_plane_provider.metadata["name"],  # type: ignore[assignment]
        }
        for infra_name, cr in infrastructure_provider_crs.items():
            providers_output[f"infrastructure_{infra_name}"] = (
                cr.metadata["name"]  # type: ignore[assignment]
            )

        self.register_outputs(
            {
                "namespace": self.namespace,
                "release_status": self.release_status,
                "provider_version": self.provider_version,
                "provider_namespaces": self.provider_namespaces,
                "providers": providers_output,
                "deployment_readiness": {
                    "core": core_deployment_ready.metadata["name"],
                    "bootstrap": bootstrap_deployment_ready.metadata["name"],
                    "control_plane": control_plane_deployment_ready.metadata[
                        "name"
                    ],
                    **{
                        f"infrastructure_{infra_name}": patch.metadata["name"]
                        for infra_name, patch in infrastructure_deployment_ready.items()
                    },
                },
                "webhook_patches": {
                    group: [patch.metadata["name"] for patch in patches]
                    for group, patches in webhook_patches.items()
                },
            }
        )

    def _provider_namespace(
        self,
        parent_name: str,
        suffix: str,
        namespace: str,
        provider: k8s.Provider | None,
    ) -> k8s.core.v1.Namespace:
        return k8s.core.v1.Namespace(
            f"{parent_name}-{suffix}-ns",
            metadata={"name": namespace},
            opts=ResourceOptions(parent=self, provider=provider),
        )

    def _provider_cr(
        self,
        parent_name: str,
        *,
        resource_name: str,
        kind: str,
        provider_name: str,
        namespace: str,
        namespace_resource: pulumi.Resource,
        dependencies: list[pulumi.Resource],
        provider: k8s.Provider | None,
        version: str = CAPI_PROVIDER_VERSION,
        infrastructure_config: InfrastructureProviderConfig | None = None,
    ) -> k8s.apiextensions.CustomResource:
        return k8s.apiextensions.CustomResource(
            f"{parent_name}-{resource_name}",
            api_version=_PROVIDER_API_VERSION,
            kind=kind,
            metadata={
                "name": provider_name,
                "namespace": namespace,
                "annotations": pulumi_wait_for(_WAIT_FOR_READY),
            },
            spec=_provider_spec(
                version=version,
                infrastructure_config=infrastructure_config,
            ),
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[*dependencies, namespace_resource],
            ),
        )

    def _provider_deployment_ready_patch(
        self,
        parent_name: str,
        *,
        resource_name: str,
        deployment_name: str,
        namespace: str,
        dependency: pulumi.Resource,
        provider: k8s.Provider | None,
    ) -> k8s.apps.v1.DeploymentPatch:
        return k8s.apps.v1.DeploymentPatch(
            f"{parent_name}-{resource_name}-controller-ready",
            metadata={
                "name": deployment_name,
                "namespace": namespace,
                "annotations": pulumi_wait_for(_WAIT_FOR_AVAILABLE),
            },
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[dependency],
            ),
        )

    def _webhook_configuration_patches(
        self,
        parent_name: str,
        *,
        resource_name: str,
        names: dict[str, str],
        dependency: pulumi.Resource,
        provider: k8s.Provider | None,
    ) -> list[_WebhookConfigurationPatch]:
        annotations = pulumi_wait_for(_WAIT_FOR_WEBHOOK_CA_BUNDLE)
        mutating = k8s.admissionregistration.v1.MutatingWebhookConfigurationPatch(
            f"{parent_name}-{resource_name}-mutating-webhook-ready",
            metadata={"name": names["mutating"], "annotations": annotations},
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[dependency],
            ),
        )
        validating = k8s.admissionregistration.v1.ValidatingWebhookConfigurationPatch(
            f"{parent_name}-{resource_name}-validating-webhook-ready",
            metadata={"name": names["validating"], "annotations": annotations},
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[dependency],
            ),
        )
        return [mutating, validating]
