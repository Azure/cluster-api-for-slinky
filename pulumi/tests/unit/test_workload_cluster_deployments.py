from __future__ import annotations

from stacks.workload_cluster.workload_cluster_deployments import (
    _cert_manager_values,
    _coredns_controller_placement_spec,
    _keda_release_name,
    _keda_scaled_object_name,
    _keda_scaled_object_spec,
    _keda_values,
    _prometheus_server_address,
    _prometheus_service_name,
    _prometheus_values,
    _slurm_nodeset_name,
    _slurm_nodeset_values,
    SlurmNodeSetSpec,
)
from stacks.workload_cluster.workload_cluster_infrastructure import (
    controller_node_affinity,
)


def test_prometheus_values_pin_components_to_controller_node() -> None:
    values = _prometheus_values()
    expected_affinity = controller_node_affinity()
    expected_tolerations = [
        {
            "key": "slinky.slurm.net/controller",
            "operator": "Exists",
            "effect": "NoSchedule",
        },
    ]

    assert values["prometheus"]["prometheusSpec"] == {
        "serviceMonitorSelectorNilUsesHelmValues": False,
        "podMonitorSelectorNilUsesHelmValues": False,
        "affinity": expected_affinity,
        "tolerations": expected_tolerations,
    }
    assert values["alertmanager"]["alertmanagerSpec"] == {
        "affinity": expected_affinity,
        "tolerations": expected_tolerations,
    }
    assert values["prometheusOperator"]["affinity"] == expected_affinity
    assert values["prometheusOperator"]["tolerations"] == expected_tolerations
    assert values["grafana"] == {
        "affinity": expected_affinity,
        "tolerations": expected_tolerations,
    }
    assert values["kube-state-metrics"] == {
        "affinity": expected_affinity,
        "tolerations": expected_tolerations,
    }


def test_keda_names_are_instance_scoped() -> None:
    assert _keda_release_name("local") == "local-keda"
    assert _keda_scaled_object_name("local", "compute") == "local-compute-nodeset-scaler"


def test_keda_values_pin_components_to_controller_node() -> None:
    values = _keda_values()
    expected_tolerations = [
        {
            "key": "slinky.slurm.net/controller",
            "operator": "Exists",
            "effect": "NoSchedule",
        },
    ]
    expected_placement = {
        "affinity": controller_node_affinity(),
        "tolerations": expected_tolerations,
    }

    assert values["affinity"] == controller_node_affinity()
    assert values["tolerations"] == expected_tolerations
    assert values["metricsServer"] == expected_placement
    assert values["webhooks"] == expected_placement


def test_cert_manager_values_pin_every_chart_pod_to_controller_node() -> None:
    values = _cert_manager_values()
    expected_placement = {
        "affinity": controller_node_affinity(),
        "tolerations": [
            {
                "key": "slinky.slurm.net/controller",
                "operator": "Exists",
                "effect": "NoSchedule",
            }
        ],
    }

    assert values["affinity"] == expected_placement["affinity"]
    assert values["tolerations"] == expected_placement["tolerations"]
    assert values["webhook"] == expected_placement
    assert values["cainjector"] == expected_placement
    assert values["startupapicheck"] == expected_placement


def test_coredns_patch_only_sets_controller_affinity() -> None:
    assert _coredns_controller_placement_spec() == {
        "template": {"spec": {"affinity": controller_node_affinity()}}
    }


def test_slurm_nodeset_values_pin_pods_to_initial_node() -> None:
    values = _slurm_nodeset_values(
        SlurmNodeSetSpec(name="compute", node_type="compute", replicas=1)
    )

    assert values["pinToNode"] is True
    assert values["taintKubeNodes"] is False
    assert values["podSpec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"][0] == {
        "key": "slinky.slurm.net/node-type",
        "operator": "In",
        "values": ["compute"],
    }
    assert values["podSpec"]["affinity"]["podAntiAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ][0]["topologyKey"] == "kubernetes.io/hostname"


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