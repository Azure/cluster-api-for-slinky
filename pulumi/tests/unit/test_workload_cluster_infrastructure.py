from __future__ import annotations

from stacks.workload_cluster.workload_cluster_infrastructure import (
    CLUSTER_AUTOSCALER_DISCOVERY_LABEL,
    CLUSTER_AUTOSCALER_DISCOVERY_LABEL_VALUE,
    _cluster_autoscaler_fullname,
    _cluster_autoscaler_kubeconfig_secret_name,
    _cluster_autoscaler_namespace,
    _cluster_autoscaler_release_name,
    _cluster_autoscaler_values,
    machine_deployment_labels,
    worker_labels,
)


def test_autoscaled_worker_labels_are_explicitly_requested() -> None:
    assert worker_labels("local-workload", "compute") == {
        "cluster.x-k8s.io/cluster-name": "local-workload",
        "slinky.slurm.net/node-type": "compute",
    }
    assert machine_deployment_labels(
        "local-workload",
        "compute",
        autoscaler_enabled=True,
    ) == {
        "cluster.x-k8s.io/cluster-name": "local-workload",
        "slinky.slurm.net/node-type": "compute",
        CLUSTER_AUTOSCALER_DISCOVERY_LABEL: CLUSTER_AUTOSCALER_DISCOVERY_LABEL_VALUE,
    }


def test_fixed_worker_labels_do_not_get_autoscaler_discovery() -> None:
    assert worker_labels("local-workload", "controller") == {
        "cluster.x-k8s.io/cluster-name": "local-workload",
        "slinky.slurm.net/node-type": "controller",
    }
    assert machine_deployment_labels("local-workload", "controller") == {
        "cluster.x-k8s.io/cluster-name": "local-workload",
        "slinky.slurm.net/node-type": "controller",
    }


def test_cluster_autoscaler_names_are_instance_scoped() -> None:
    assert _cluster_autoscaler_namespace("local") == "local-autoscaler"
    assert _cluster_autoscaler_release_name("local") == "local-autoscaler"
    assert _cluster_autoscaler_fullname("local") == "local-cluster-autoscaler"
    assert (
        _cluster_autoscaler_kubeconfig_secret_name("local")
        == "local-autoscaler-kubeconfig"
    )


def test_cluster_autoscaler_values_use_secret_backed_workload_kubeconfig() -> None:
    values = _cluster_autoscaler_values(
        cluster_name="local-workload",
        fullname="local-cluster-autoscaler",
        kubeconfig_secret_name="local-autoscaler-kubeconfig",
    )

    assert values["fullnameOverride"] == "local-cluster-autoscaler"
    assert values["cloudProvider"] == "clusterapi"
    assert values["clusterAPIMode"] == "kubeconfig-incluster"
    assert values["clusterAPIKubeconfigSecret"] == "local-autoscaler-kubeconfig"
    assert values["clusterAPIWorkloadKubeconfigPath"] == "/etc/kubernetes/value"
    assert values["autoDiscovery"] == {
        "namespace": "default",
        "clusterName": "local-workload",
    }
    assert values["extraArgs"] == {
        "logtostderr": True,
        "stderrthreshold": "info",
        "v": 4,
        "scale-down-unneeded-time": "2m",
    }