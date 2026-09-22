# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Reusable addons installed onto CAPI-managed workload clusters."""

from __future__ import annotations

import urllib.request
from typing import Any

import pulumi
import pulumi_kubernetes as k8s
import yaml

from stacks.workload_cluster.workload_cluster_infrastructure import (
    controller_node_selector,
    controller_tolerations,
)


_AZURE_CCM_CHART_REPO = (
    "https://raw.githubusercontent.com/kubernetes-sigs/cloud-provider-azure/master/helm/repo"
)
_AZURE_CCM_CHART_NAME = "cloud-provider-azure"
_AZURE_CCM_CHART_VERSION = "1.36.0"
_AZURE_CCM_RELEASE_NAME = "cloud-provider-azure-oot"

_CALICO_VERSION = "v3.32.0"
_CALICO_MANIFEST_URL = (
    "https://raw.githubusercontent.com/projectcalico/calico/"
    f"{_CALICO_VERSION}/manifests/calico.yaml"
)


def _calico_manifest_objects(manifest: str, *, pod_cidr: str, vxlan_mode: str) -> list[dict[str, Any]]:
    objects = [obj for obj in yaml.safe_load_all(manifest) if obj is not None]
    for obj in objects:
        kind, name = obj["kind"], obj["metadata"]["name"]
        if (kind, name) == ("ConfigMap", "calico-config"):
            obj["data"]["calico_backend"] = "vxlan"
            obj["data"]["typha_service_name"] = "none"
        elif (kind, name) == ("DaemonSet", "calico-node"):
            pod = obj["spec"]["template"]["spec"]
            container = next(item for item in pod["containers"] if item["name"] == "calico-node")
            overrides = {
                "CLUSTER_TYPE": "k8s",
                "CALICO_IPV4POOL_CIDR": pod_cidr,
                "CALICO_IPV4POOL_IPIP": "Never",
                "CALICO_IPV4POOL_VXLAN": vxlan_mode,
                "IP_AUTODETECTION_METHOD": "kubernetes-internal-ip",
            }
            container["env"] = [item for item in container["env"] if item["name"] not in overrides]
            container["env"].extend({"name": name, "value": value} for name, value in overrides.items())
            container["livenessProbe"]["exec"]["command"] = ["/bin/calico-node", "-felix-live"]
            container["readinessProbe"]["exec"]["command"] = ["/bin/calico-node", "-felix-ready"]
        elif (kind, name) == ("Deployment", "calico-kube-controllers"):
            obj["spec"]["replicas"] = 1
            pod = obj["spec"]["template"]["spec"]
            pod["nodeSelector"].update(controller_node_selector())
            pod["tolerations"] = controller_tolerations()
    return objects


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


def _read_url(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8")


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


class CalicoCNI(pulumi.ComponentResource):
    """Manifest-managed Calico with direct Kubernetes datastore access."""

    version: pulumi.Output[str]
    status: pulumi.Output[Any]

    def __init__(
        self,
        name: str,
        *,
        pod_cidr: str,
        provider: k8s.Provider,
        vxlan_mode: str = "Always",
        depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:workload:CalicoCNI", name, props={}, opts=opts)

        def retain_manifest(obj: dict[str, Any], resource_options: pulumi.ResourceOptions) -> None:
            resource_options.retain_on_delete = True

        manifests = k8s.yaml.ConfigGroup(
            "calico-manifests",
            yaml=[yaml.safe_dump_all(_calico_manifest_objects(
                _read_url(_CALICO_MANIFEST_URL), pod_cidr=pod_cidr, vxlan_mode=vxlan_mode,
            ))],
            transformations=[retain_manifest],
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
                retain_on_delete=True,
            ),
        )
        self.version = pulumi.Output.from_input(_CALICO_VERSION)
        self.status = manifests.resources.apply(
            lambda resources: pulumi.Output.all(*[resource.id for resource in resources.values()])
        ).apply(lambda _: {"version": _CALICO_VERSION, "status": "deployed", "typha": False})
        self.register_outputs({"version": self.version, "status": self.status})
