# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Shared workload-cluster addon value rendering tests."""

from __future__ import annotations

import pytest
import yaml
import pulumi
import pulumi_kubernetes as k8s
from stacks.workload_cluster import workload_cluster_addons as addons

from stacks.workload_cluster.workload_cluster_addons import (
    _azure_cloud_provider_values,
    _calico_manifest_objects,
)
from stacks.workload_cluster.workload_cluster_infrastructure import (
    controller_tolerations,
)


@pytest.mark.parametrize("vxlan_mode", ["Always", "CrossSubnet"])
def test_calico_manifest_uses_direct_datastore_and_one_controller(vxlan_mode) -> None:
    objects = [
        {"kind": "ConfigMap", "metadata": {"name": "calico-config"}, "data": {
            "calico_backend": "bird", "typha_service_name": "none", "cni_network_config": "preserved",
        }},
        {"kind": "DaemonSet", "metadata": {"name": "calico-node"}, "spec": {"template": {"spec": {
            "hostNetwork": True, "tolerations": [{"operator": "Exists"}], "containers": [{
                "name": "calico-node", "env": [{"name": "DATASTORE_TYPE", "value": "kubernetes"},
                                             {"name": "CALICO_IPV4POOL_IPIP", "value": "Always"}],
                "livenessProbe": {"exec": {"command": ["/bin/calico-node", "-bird-live"]}},
                "readinessProbe": {"exec": {"command": ["/bin/calico-node", "-bird-ready"]}},
            }],
        }}}},
        {"kind": "Deployment", "metadata": {"name": "calico-kube-controllers"}, "spec": {
            "replicas": 1, "template": {"spec": {"nodeSelector": {"kubernetes.io/os": "linux"}}},
        }},
    ]
    rendered = _calico_manifest_objects(yaml.safe_dump_all(objects), pod_cidr="192.168.0.0/16", vxlan_mode=vxlan_mode)
    config, daemon, controllers = rendered
    assert config["data"] == {"calico_backend": "vxlan", "typha_service_name": "none", "cni_network_config": "preserved"}
    pod = daemon["spec"]["template"]["spec"]
    assert pod["tolerations"] == [{"operator": "Exists"}]
    container = pod["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["DATASTORE_TYPE"] == "kubernetes"
    assert env["CALICO_IPV4POOL_CIDR"] == "192.168.0.0/16"
    assert env["CALICO_IPV4POOL_IPIP"] == "Never"
    assert env["CALICO_IPV4POOL_VXLAN"] == vxlan_mode
    assert env["IP_AUTODETECTION_METHOD"] == "kubernetes-internal-ip"
    assert not any("TYPHA" in key for key in env)
    assert container["readinessProbe"]["exec"]["command"] == ["/bin/calico-node", "-felix-ready"]
    assert container["livenessProbe"]["exec"]["command"] == ["/bin/calico-node", "-felix-live"]
    assert controllers["spec"]["replicas"] == 1
    assert controllers["spec"]["template"]["spec"]["nodeSelector"] == {
        "kubernetes.io/os": "linux", "slinky.slurm.net/node-type": "controller",
    }
    assert controllers["spec"]["template"]["spec"]["tolerations"] == controller_tolerations()


def test_azure_cloud_provider_values_cover_bootstrap_taints() -> None:
    values = _azure_cloud_provider_values(
        cluster_name="caps-self",
        pod_cidr="192.168.0.0/16",
    )

    assert values["infra"] == {"clusterName": "caps-self"}
    ccm = values["cloudControllerManager"]
    assert ccm["clusterCIDR"] == "192.168.0.0/16"
    assert ccm["configureCloudRoutes"] == "true"
    assert {
        toleration["key"]
        for toleration in ccm["tolerations"]
    } >= {
        "node-role.kubernetes.io/control-plane",
        "node.cloudprovider.kubernetes.io/uninitialized",
        "node.kubernetes.io/not-ready",
    }


def test_calico_component_retains_resources_and_waits_for_manifests(monkeypatch) -> None:
    resources = []
    options = {}
    manifest = yaml.safe_dump({
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "calico-config", "namespace": "kube-system"},
        "data": {},
    })
    monkeypatch.setattr(addons, "_read_url", lambda url: manifest)

    class Mocks(pulumi.runtime.Mocks):
        def new_resource(self, args):
            resources.append(args)
            return args.name, args.inputs

        def call(self, args):
            assert args.token == "kubernetes:yaml:decode"
            return {"result": list(yaml.safe_load_all(args.args["text"]))}

    def record(args):
        options[args.name] = args.opts

    pulumi.runtime.set_mocks(Mocks())

    @pulumi.runtime.test
    def check():
        component = addons.CalicoCNI(
            "calico", pod_cidr="192.168.0.0/16", provider=k8s.Provider("workload", kubeconfig="{}"),
            opts=pulumi.ResourceOptions(transformations=[record]),
        )

        def verify(status):
            assert status == {"version": "v3.32.0", "status": "deployed", "typha": False}
            config = next(resource for resource in resources if resource.typ == "kubernetes:core/v1:ConfigMap")
            assert options[config.name].retain_on_delete
            assert not any(resource.typ == "kubernetes:helm.sh/v3:Release" for resource in resources)
        return component.status.apply(verify)

    check()
