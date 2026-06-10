from __future__ import annotations

from stacks.workload_cluster.workload_cluster_local_local import (
    _AUTOSCALER_MAX_ANNOTATION,
    _AUTOSCALER_MIN_ANNOTATION,
    _CLUSTER_AUTOSCALER_DISCOVERY_LABEL,
    _CLUSTER_AUTOSCALER_DISCOVERY_LABEL_VALUE,
    WorkerClassSpec,
    _autoscaled_worker_classes,
    _cluster_autoscaler_fullname,
    _cluster_autoscaler_kubeconfig_secret_name,
    _cluster_autoscaler_namespace,
    _cluster_autoscaler_release_name,
    _cluster_autoscaler_values,
    _machine_deployment_labels,
    _prometheus_values,
    _worker_labels,
)


def test_autoscaled_worker_class_is_discoverable_without_fixed_replicas() -> None:
    worker = WorkerClassSpec(
        name="compute",
        node_type="compute",
        replicas=None,
        annotations={
            _AUTOSCALER_MIN_ANNOTATION: "1",
            _AUTOSCALER_MAX_ANNOTATION: "10",
        },
    )

    assert _autoscaled_worker_classes((worker,)) == (worker,)
    assert _worker_labels("local-workload", worker) == {
        "cluster.x-k8s.io/cluster-name": "local-workload",
        "slinky.slurm.net/node-type": "compute",
    }
    assert _machine_deployment_labels("local-workload", worker) == {
        "cluster.x-k8s.io/cluster-name": "local-workload",
        "slinky.slurm.net/node-type": "compute",
        _CLUSTER_AUTOSCALER_DISCOVERY_LABEL: _CLUSTER_AUTOSCALER_DISCOVERY_LABEL_VALUE,
    }


def test_fixed_worker_class_is_not_autoscaler_discovered() -> None:
    worker = WorkerClassSpec(name="head", node_type="controller", replicas=1)

    assert _autoscaled_worker_classes((worker,)) == ()
    assert _worker_labels("local-workload", worker) == {
        "cluster.x-k8s.io/cluster-name": "local-workload",
        "slinky.slurm.net/node-type": "controller",
    }
    assert _machine_deployment_labels("local-workload", worker) == {
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


def test_prometheus_values_pin_components_to_controller_node() -> None:
    values = _prometheus_values()
    expected_node_selector = {"slinky.slurm.net/node-type": "controller"}
    expected_tolerations = [
        {
            "key": "slinky.slurm.net/controller",
            "operator": "Exists",
            "effect": "NoSchedule",
        }
    ]

    assert values["prometheus"]["prometheusSpec"] == {
        "serviceMonitorSelectorNilUsesHelmValues": False,
        "podMonitorSelectorNilUsesHelmValues": False,
        "nodeSelector": expected_node_selector,
        "tolerations": expected_tolerations,
    }
    assert values["alertmanager"]["alertmanagerSpec"] == {
        "nodeSelector": expected_node_selector,
        "tolerations": expected_tolerations,
    }
    assert values["prometheusOperator"]["nodeSelector"] == expected_node_selector
    assert values["prometheusOperator"]["tolerations"] == expected_tolerations
    assert values["grafana"] == {
        "nodeSelector": expected_node_selector,
        "tolerations": expected_tolerations,
    }
    assert values["kube-state-metrics"] == {
        "nodeSelector": expected_node_selector,
        "tolerations": expected_tolerations,
    }
