# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Reusable addons installed onto CAPI-managed workload clusters."""

from __future__ import annotations

import urllib.request
from typing import Any

import pulumi
import pulumi_kubernetes as k8s

from stacks.workload_cluster.workload_cluster_infrastructure import (
    controller_bootstrap_tolerations,
    controller_node_affinity,
    controller_node_selector,
    controller_tolerations,
)


_AZURE_CCM_CHART_REPO = (
    "https://raw.githubusercontent.com/kubernetes-sigs/cloud-provider-azure/master/helm/repo"
)
_AZURE_CCM_CHART_NAME = "cloud-provider-azure"
_AZURE_CCM_CHART_VERSION = "1.36.0"
_AZURE_CCM_RELEASE_NAME = "cloud-provider-azure-oot"

_CALICO_CHART_REPO = "https://docs.tigera.io/calico/charts"
_CALICO_CHART_NAME = "tigera-operator"
_CALICO_CHART_VERSION = "v3.32.0"
_CALICO_OPERATOR_CRDS_URL = (
    "https://raw.githubusercontent.com/projectcalico/calico/"
    f"{_CALICO_CHART_VERSION}/manifests/operator-crds.yaml"
)
_CALICO_OPERATOR_NAMESPACE = "tigera-operator"


def _azure_cloud_provider_values(
    *,
    cluster_name: str,
    pod_cidr: str,
) -> dict[str, object]:
    return {
        "infra": {"clusterName": cluster_name},
        "cloudControllerManager": {
            "clusterCIDR": pod_cidr,
            "configureCloudRoutes": "true",
            "logVerbosity": "4",
            "tolerations": [
                {
                    "key": "node-role.kubernetes.io/control-plane",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                },
                {
                    "key": "node-role.kubernetes.io/etcd",
                    "operator": "Exists",
                    "effect": "NoExecute",
                },
                {
                    "key": "node.cloudprovider.kubernetes.io/uninitialized",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                },
                {
                    "key": "node.kubernetes.io/not-ready",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                },
            ],
        },
    }


def _calico_vxlan_values(*, pod_cidr: str) -> dict[str, object]:
    return {
        "nodeSelector": controller_node_selector(),
        "affinity": controller_node_affinity(),
        "tolerations": controller_bootstrap_tolerations(),
        "installation": {
            "controlPlaneNodeSelector": controller_node_selector(),
            "controlPlaneTolerations": controller_tolerations(),
            "calicoNetwork": {
                "ipPools": [
                    {
                        "name": "default-ipv4-ippool",
                        "blockSize": 26,
                        "cidr": pod_cidr,
                        "encapsulation": "VXLAN",
                        "natOutgoing": "Enabled",
                        "nodeSelector": "all()",
                    }
                ],
            },
        },
    }


def _read_url(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8")


def _calico_operator_crd_dependencies(
    calico_operator_crds: k8s.yaml.ConfigGroup,
) -> list[pulumi.Input[pulumi.Resource]]:
    return [
        calico_operator_crds.get_resource(
            "apiextensions.k8s.io/v1/CustomResourceDefinition",
            name,
        )
        for name in (
            "apiservers.operator.tigera.io",
            "goldmanes.operator.tigera.io",
            "installations.operator.tigera.io",
            "whiskers.operator.tigera.io",
        )
    ]


class AzureCloudProvider(pulumi.ComponentResource):
    """Out-of-tree Azure cloud controller and cloud node manager."""

    chart_version: pulumi.Output[str]
    status: pulumi.Output[Any]

    def __init__(
        self,
        name: str,
        *,
        cluster_name: str,
        pod_cidr: str,
        provider: k8s.Provider,
        depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:workload:AzureCloudProvider", name, props={}, opts=opts)

        release = k8s.helm.v3.Release(
            "release",
            name=_AZURE_CCM_RELEASE_NAME,
            chart=_AZURE_CCM_CHART_NAME,
            version=_AZURE_CCM_CHART_VERSION,
            repository_opts={"repo": _AZURE_CCM_CHART_REPO},
            namespace="kube-system",
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=900,
            values=_azure_cloud_provider_values(
                cluster_name=cluster_name,
                pod_cidr=pod_cidr,
            ),
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
                retain_on_delete=True,
            ),
        )
        self.chart_version = pulumi.Output.from_input(_AZURE_CCM_CHART_VERSION)
        self.status = release.status
        self.register_outputs(
            {"chart_version": self.chart_version, "status": self.status}
        )


class CalicoVXLAN(pulumi.ComponentResource):
    """Calico networking with VXLAN encapsulation on every pod-network path."""

    chart_version: pulumi.Output[str]
    status: pulumi.Output[Any]

    def __init__(
        self,
        name: str,
        *,
        pod_cidr: str,
        provider: k8s.Provider,
        depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:workload:CalicoVXLAN", name, props={}, opts=opts)

        namespace = k8s.core.v1.Namespace(
            "namespace",
            metadata={
                "name": _CALICO_OPERATOR_NAMESPACE,
                "labels": {"pod-security.kubernetes.io/enforce": "privileged"},
            },
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
                retain_on_delete=True,
            ),
        )
        operator_crds = k8s.yaml.ConfigGroup(
            "operator-crds",
            yaml=[_read_url(_CALICO_OPERATOR_CRDS_URL)],
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[namespace],
                retain_on_delete=True,
            ),
        )
        release = k8s.helm.v3.Release(
            "operator",
            chart=_CALICO_CHART_NAME,
            version=_CALICO_CHART_VERSION,
            repository_opts={"repo": _CALICO_CHART_REPO},
            namespace=_CALICO_OPERATOR_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            skip_crds=True,
            timeout=900,
            values=_calico_vxlan_values(pod_cidr=pod_cidr),
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[
                    namespace,
                    *_calico_operator_crd_dependencies(operator_crds),
                ],
                retain_on_delete=True,
            ),
        )
        self.chart_version = pulumi.Output.from_input(_CALICO_CHART_VERSION)
        self.status = release.status
        self.register_outputs(
            {"chart_version": self.chart_version, "status": self.status}
        )
