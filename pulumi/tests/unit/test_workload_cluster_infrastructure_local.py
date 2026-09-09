# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import base64

import yaml

from stacks.kubernetes_annotations import (
    DELETE_PROPAGATION_FOREGROUND,
    PULUMI_DELETION_PROPAGATION_POLICY_ANNOTATION,
    foreground_delete_annotations,
)
from stacks.workload_cluster.workload_cluster_infrastructure import (
    AUTOSCALER_MAX_ANNOTATION,
    AUTOSCALER_MIN_ANNOTATION,
    CONTROLLER_NODE_TYPE,
    calico_typha_deployment,
    controller_taint,
)
from stacks.workload_cluster.workload_cluster_class_local import (
    _LOCAL_MACHINE_DEPLOYMENTS,
)
from stacks.workload_cluster.workload_cluster_infrastructure_local import (
    _NODE_UNHEALTHY_TIMEOUT_SECONDS,
    _SERVICE_ACCOUNT_TOKEN_PATH,
    _WAIT_FOR_CONTROL_PLANE_AVAILABLE,
    _calico_values,
    _containerd_registry_commands,
    _health_check,
    _management_kubeconfig,
    _node_registration,
)
from stacks.workload_cluster.registry_setting import (
    ContainerdHostConfig,
    ContainerdRegistryConfig,
    LocalRegistryConfig,
)


def test_foreground_delete_annotations_preserve_existing_annotations() -> None:
    annotations = foreground_delete_annotations(
        {
            AUTOSCALER_MIN_ANNOTATION: "1",
            AUTOSCALER_MAX_ANNOTATION: "10",
        }
    )

    assert annotations == {
        AUTOSCALER_MIN_ANNOTATION: "1",
        AUTOSCALER_MAX_ANNOTATION: "10",
        PULUMI_DELETION_PROPAGATION_POLICY_ANNOTATION: (
            DELETE_PROPAGATION_FOREGROUND
        ),
    }


def test_v1beta1_cluster_wait_uses_legacy_control_plane_condition() -> None:
    assert _WAIT_FOR_CONTROL_PLANE_AVAILABLE == "condition=ControlPlaneReady"


def test_local_health_check_allows_initial_addon_convergence() -> None:
    assert _NODE_UNHEALTHY_TIMEOUT_SECONDS == 900
    assert _health_check() == {
        "checks": {
            "unhealthyNodeConditions": [
                {"type": "Ready", "status": "Unknown", "timeoutSeconds": 900},
                {"type": "Ready", "status": "False", "timeoutSeconds": 900},
            ]
        }
    }


def test_management_kubeconfig_reads_current_service_account_token() -> None:
    kubeconfig = yaml.safe_load(
        _management_kubeconfig("https://10.96.0.1:443", "test-ca")
    )

    assert kubeconfig["clusters"][0]["cluster"] == {
        "server": "https://10.96.0.1:443",
        "certificate-authority-data": base64.b64encode(b"test-ca").decode(),
    }
    assert kubeconfig["users"][0]["user"] == {
        "exec": {
            "apiVersion": "client.authentication.k8s.io/v1",
            "command": "/bin/sh",
            "args": [
                "-c",
                (
                    "printf '{\"apiVersion\":\"client.authentication.k8s.io/v1\","
                    "\"kind\":\"ExecCredential\",\"status\":{\"token\":\"%s\"}}\\n' "
                    f'"$(cat {_SERVICE_ACCOUNT_TOKEN_PATH})"'
                ),
            ],
            "interactiveMode": "Never",
        }
    }


def test_local_topology_has_fixed_head_and_autoscaled_compute_deployments() -> None:
    assert [worker.name for worker in _LOCAL_MACHINE_DEPLOYMENTS] == [
        "head",
        "compute",
    ]


def test_local_control_plane_registration_has_no_custom_label_or_taint() -> None:
    registration = _node_registration()

    assert registration == {
        "kubeletExtraArgs": [
            {
                "name": "eviction-hard",
                "value": (
                    "nodefs.available<0%,nodefs.inodesFree<0%,"
                    "imagefs.available<0%"
                ),
            },
        ]
    }


def test_local_controller_worker_registration_adds_critical_addons_taint() -> None:
    registration = _node_registration(CONTROLLER_NODE_TYPE)

    assert registration["taints"] == [controller_taint()]
    assert registration["kubeletExtraArgs"][-1] == {
        "name": "node-labels",
        "value": "slinky.slurm.net/node-type=controller",
    }


def test_local_calico_pins_typha_to_controller_nodes() -> None:
    values = _calico_values()

    assert values["installation"][
        "typhaDeployment"
    ] == calico_typha_deployment()


def test_registries_redirect_logical_names_to_host_ports() -> None:
    commands = _containerd_registry_commands(
        (
            LocalRegistryConfig(
                registry="docker.io",
                config=ContainerdRegistryConfig(
                    server="https://registry-1.docker.io",
                    hosts=(ContainerdHostConfig(gateway_port=5002),),
                ),
            ),
            LocalRegistryConfig(
                registry="custom-registry:5000",
                config=ContainerdRegistryConfig(
                    server="http://custom-registry:5000",
                    hosts=(
                        ContainerdHostConfig(
                            gateway_port=5003,
                            capabilities=("pull", "resolve", "push"),
                        ),
                    ),
                ),
            ),
        )
    )

    assert commands[0] == (
        "mkdir -p /etc/containerd/certs.d/docker.io "
        "/etc/containerd/certs.d/custom-registry:5000"
    )
    assert commands[1].count("_CA4S_REGISTRY_HOST=host.docker.internal") == 1
    assert 'server = "https://registry-1.docker.io"' in commands[1]
    assert '[host."http://${_CA4S_REGISTRY_HOST}:5002"]' in commands[1]
    assert 'server = "http://custom-registry:5000"' in commands[1]
    assert '[host."http://${_CA4S_REGISTRY_HOST}:5003"]' in commands[1]
    assert 'capabilities = ["pull", "resolve", "push"]' in commands[1]
    assert commands[-1] == "systemctl restart containerd"


def test_empty_registry_list_writes_no_containerd_overrides() -> None:
    assert _containerd_registry_commands(()) == []
