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


CAPI_OPERATOR_CHART_REPO = "https://kubernetes-sigs.github.io/cluster-api-operator"
CAPI_OPERATOR_CHART_NAME = "cluster-api-operator"
CAPI_OPERATOR_CHART_VERSION = "0.27.0"
CAPI_OPERATOR_RELEASE_NAME = "cluster-api-operator"

# Keep these aligned with the clusterctl binary used for local validation and
# with the v1beta2 manifests in capi-quickstart.yaml.
CAPI_PROVIDER_VERSION = "v1.12.8"

CAPI_OPERATOR_NAMESPACE = "capi-operator-system"
CAPI_CORE_NAMESPACE = "capi-system"
CAPI_BOOTSTRAP_NAMESPACE = "kubeadm-bootstrap-system"
CAPI_CONTROL_PLANE_NAMESPACE = "kubeadm-control-plane-system"
CAPI_DOCKER_INFRASTRUCTURE_NAMESPACE = "docker-infrastructure-system"

_PROVIDER_API_VERSION = "operator.cluster.x-k8s.io/v1alpha2"
_PROVIDER_FEATURE_GATES = {
    "ClusterTopology": True,
}
_WAIT_FOR_ANNOTATION = "pulumi.com/waitFor"
_WAIT_FOR_READY = "condition=Ready"
_WAIT_FOR_WEBHOOK_CA_BUNDLE = "jsonpath={.webhooks[*].clientConfig.caBundle}"

_CAPI_OPERATOR_WEBHOOK_CONFIGURATIONS = {
    "mutating": "capi-operator-mutating-webhook-configuration",
    "validating": "capi-operator-validating-webhook-configuration",
}

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
    "infrastructure": {
        "mutating": "capd-mutating-webhook-configuration",
        "validating": "capd-validating-webhook-configuration",
    },
}


class ClusterAPIOperator(pulumi.ComponentResource):
    """Install Cluster API Operator and local CAPI providers.

    Outputs:
      * ``namespace`` — namespace containing the operator deployment.
      * ``release_status`` — Helm release status.
      * ``provider_version`` — pinned CAPI provider version.
      * ``provider_namespaces`` — namespaces containing provider CRs and
        reconciled controllers.
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

        core_ns = self._provider_namespace(name, "core", CAPI_CORE_NAMESPACE, provider)
        bootstrap_ns = self._provider_namespace(
            name, "bootstrap", CAPI_BOOTSTRAP_NAMESPACE, provider
        )
        control_plane_ns = self._provider_namespace(
            name, "control-plane", CAPI_CONTROL_PLANE_NAMESPACE, provider
        )
        infrastructure_ns = self._provider_namespace(
            name, "docker-infra", CAPI_DOCKER_INFRASTRUCTURE_NAMESPACE, provider
        )

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
        infrastructure_provider = self._provider_cr(
            name,
            resource_name="infrastructure-docker",
            kind="InfrastructureProvider",
            provider_name="docker",
            namespace=CAPI_DOCKER_INFRASTRUCTURE_NAMESPACE,
            namespace_resource=infrastructure_ns,
            dependencies=[release, *operator_webhooks],
            provider=provider,
        )
        core_webhooks = self._webhook_configuration_patches(
            name,
            resource_name="core",
            names=_PROVIDER_WEBHOOK_CONFIGURATIONS["core"],
            dependency=core_provider,
            provider=provider,
        )
        bootstrap_webhooks = self._webhook_configuration_patches(
            name,
            resource_name="bootstrap",
            names=_PROVIDER_WEBHOOK_CONFIGURATIONS["bootstrap"],
            dependency=bootstrap_provider,
            provider=provider,
        )
        control_plane_webhooks = self._webhook_configuration_patches(
            name,
            resource_name="control-plane",
            names=_PROVIDER_WEBHOOK_CONFIGURATIONS["control_plane"],
            dependency=control_plane_provider,
            provider=provider,
        )
        infrastructure_webhooks = self._webhook_configuration_patches(
            name,
            resource_name="infrastructure",
            names=_PROVIDER_WEBHOOK_CONFIGURATIONS["infrastructure"],
            dependency=infrastructure_provider,
            provider=provider,
        )
        webhook_patches = {
            "operator": operator_webhooks,
            "core": core_webhooks,
            "bootstrap": bootstrap_webhooks,
            "control_plane": control_plane_webhooks,
            "infrastructure": infrastructure_webhooks,
        }

        self.namespace = Output.from_input(CAPI_OPERATOR_NAMESPACE)
        self.release_status = release.status
        self.provider_version = Output.from_input(CAPI_PROVIDER_VERSION)
        self.provider_namespaces = {
            "core": Output.from_input(CAPI_CORE_NAMESPACE),
            "bootstrap": Output.from_input(CAPI_BOOTSTRAP_NAMESPACE),
            "control_plane": Output.from_input(CAPI_CONTROL_PLANE_NAMESPACE),
            "infrastructure": Output.from_input(CAPI_DOCKER_INFRASTRUCTURE_NAMESPACE),
        }

        self.register_outputs(
            {
                "namespace": self.namespace,
                "release_status": self.release_status,
                "provider_version": self.provider_version,
                "provider_namespaces": self.provider_namespaces,
                "providers": {
                    "core": core_provider.metadata["name"],
                    "bootstrap": bootstrap_provider.metadata["name"],
                    "control_plane": control_plane_provider.metadata["name"],
                    "infrastructure": infrastructure_provider.metadata["name"],
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
    ) -> k8s.apiextensions.CustomResource:
        return k8s.apiextensions.CustomResource(
            f"{parent_name}-{resource_name}",
            api_version=_PROVIDER_API_VERSION,
            kind=kind,
            metadata={
                "name": provider_name,
                "namespace": namespace,
                "annotations": {_WAIT_FOR_ANNOTATION: _WAIT_FOR_READY},
            },
            spec={
                "version": CAPI_PROVIDER_VERSION,
                "manager": {
                    "featureGates": _PROVIDER_FEATURE_GATES,
                },
            },
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[*dependencies, namespace_resource],
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
    ) -> list[pulumi.Resource]:
        annotations = {_WAIT_FOR_ANNOTATION: _WAIT_FOR_WEBHOOK_CA_BUNDLE}
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
