from __future__ import annotations

from stacks.workload_cluster.workload_cluster_deployments import (
    _keda_release_name,
    _keda_scaled_object_name,
    _keda_scaled_object_spec,
    _keda_values,
    _prometheus_server_address,
    _prometheus_service_name,
    _prometheus_values,
    _slurm_nodeset_name,
)


def test_prometheus_values_pin_components_to_controller_node() -> None:
    values = _prometheus_values()
    expected_node_selector = {"slinky.slurm.net/node-type": "controller"}
    expected_tolerations = [
        {
            "key": "slinky.slurm.net/controller",
            "operator": "Exists",
            "effect": "NoSchedule",
        },
        {
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "Exists",
            "effect": "NoSchedule",
        },
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


def test_keda_names_are_instance_scoped() -> None:
    assert _keda_release_name("local") == "local-keda"
    assert _keda_scaled_object_name("local", "compute") == "local-compute-nodeset-scaler"


def test_keda_values_pin_components_to_controller_node() -> None:
    values = _keda_values()
    expected_node_selector = {"slinky.slurm.net/node-type": "controller"}
    expected_tolerations = [
        {
            "key": "slinky.slurm.net/controller",
            "operator": "Exists",
            "effect": "NoSchedule",
        },
        {
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "Exists",
            "effect": "NoSchedule",
        },
    ]
    expected_placement = {
        "nodeSelector": expected_node_selector,
        "tolerations": expected_tolerations,
    }

    assert values["nodeSelector"] == expected_node_selector
    assert values["tolerations"] == expected_tolerations
    assert values["metricsServer"] == expected_placement
    assert values["webhooks"] == expected_placement


def test_prometheus_server_address_uses_helm_release_name() -> None:
    assert (
        _prometheus_service_name("prometheus-a9b5a75d")
        == "prometheus-a9b5a75d-kube-p-prometheus"
    )
    assert _prometheus_server_address("prometheus-a9b5a75d") == (
        "http://prometheus-a9b5a75d-kube-p-prometheus."
        "prometheus.svc.cluster.local:9090"
    )


def test_keda_scaled_object_targets_slurm_nodeset() -> None:
    assert _slurm_nodeset_name("slurm-87aab368", "compute") == (
        "slurm-87aab368-worker-compute"
    )

    spec = _keda_scaled_object_spec(
        node_set_name="slurm-87aab368-worker-compute",
        min_replicas=1,
        max_replicas=10,
        prometheus_server_address=(
            "http://prometheus-a9b5a75d-kube-p-prometheus."
            "prometheus.svc.cluster.local:9090"
        ),
    )

    assert spec == {
        "scaleTargetRef": {
            "apiVersion": "slinky.slurm.net/v1beta1",
            "kind": "NodeSet",
            "name": "slurm-87aab368-worker-compute",
        },
        "minReplicaCount": 1,
        "maxReplicaCount": 10,
        "triggers": [
            {
                "type": "prometheus",
                "metadata": {
                    "serverAddress": (
                        "http://prometheus-a9b5a75d-kube-p-prometheus."
                        "prometheus.svc.cluster.local:9090"
                    ),
                    "query": 'sum(slurm_partition_jobs_pending{partition="all"})',
                    "threshold": "1",
                    "activationThreshold": "1",
                    "unsafeSsl": "true",
                },
            }
        ],
    }