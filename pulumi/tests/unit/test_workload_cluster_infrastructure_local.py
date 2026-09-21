# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import base64
import subprocess
import tomllib

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
    controller_taint,
)
from stacks.workload_cluster.workload_cluster_class_local import (
    _LOCAL_MACHINE_DEPLOYMENTS,
)
from stacks.workload_cluster.workload_cluster_infrastructure_local import (
    _NODE_UNHEALTHY_TIMEOUT_SECONDS,
    _SERVICE_ACCOUNT_TOKEN_PATH,
    _WAIT_FOR_CONTROL_PLANE_AVAILABLE,
    _cluster_configuration,
    _containerd_registry_commands,
    _local_registry_routes,
    _health_check,
    _management_kubeconfig,
    _node_registration,
)
from stacks.workload_cluster.registry_setting import (
    ContainerdHostConfig, ContainerdRegistryConfig, LocalCustomRegistrySetting, LocalPortRegistrySetting,
)
from stacks.workload_cluster import workload_cluster_infrastructure_local as local_infra


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


def test_v1beta2_cluster_wait_uses_control_plane_available_condition() -> None:
    assert _WAIT_FOR_CONTROL_PLANE_AVAILABLE == "condition=ControlPlaneAvailable"


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


def test_local_control_plane_enables_native_podgroups() -> None:
    configuration = _cluster_configuration()
    feature_gates = [{
        "name": "feature-gates",
        "value": "GenericWorkload=true,WorkloadWithJob=true",
    }]
    assert configuration == {
        "apiServer": {
            "certSANs": ["localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"],
            "extraArgs": [
                *feature_gates,
                {"name": "runtime-config", "value": "scheduling.k8s.io/v1alpha2=true"},
            ],
        },
        "controllerManager": {"extraArgs": feature_gates},
        "scheduler": {"extraArgs": feature_gates},
    }


def test_local_controller_worker_registration_adds_critical_addons_taint() -> None:
    registration = _node_registration(CONTROLLER_NODE_TYPE)

    assert registration["taints"] == [controller_taint()]
    assert registration["kubeletExtraArgs"][-1] == {
        "name": "node-labels",
        "value": "slinky.slurm.net/node-type=controller",
    }


def test_containerd_routes_render_multiple_hosts_and_restart_once(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(local_infra, "_CONTAINERD_CERTS_DIR", str(tmp_path))
    routes = _local_registry_routes(
        LocalPortRegistrySetting(port=5002), LocalCustomRegistrySetting(registry_name="custom-registry", port=5003),
        {"private.example": ContainerdRegistryConfig(server="https://private.example", hosts=(
            ContainerdHostConfig(url="https://mirror.example", capabilities=("pull",)),
            ContainerdHostConfig(gateway_port=5443, scheme="https", capabilities=("pull", "resolve", "push")),
        ))},
    )
    commands = _containerd_registry_commands(routes)
    assert sum("getent hosts" in command for command in commands) == 1
    assert commands.count("systemctl restart containerd") == 1
    script = "\n".join(commands)
    subprocess.run(["sh", "-n"], input=script, text=True, check=True)
    for _ in range(2):
        subprocess.run(["sh", "-eu"], input=(
            "getent() { return 1; }\nip() { echo 'default via 172.17.0.1'; }\n"
            "systemctl() { :; }\n" + script
        ), text=True, check=True)
    docker = tomllib.loads((tmp_path / "docker.io/hosts.toml").read_text())
    assert docker["host"] == {"http://172.17.0.1:5002": {"capabilities": ["pull", "resolve"]}}
    custom = tomllib.loads((tmp_path / "custom-registry:5000/hosts.toml").read_text())
    assert custom["server"] == "http://custom-registry:5000"
    assert "http://172.17.0.1:5003" in custom["host"]
    private = tomllib.loads((tmp_path / "private.example/hosts.toml").read_text())
    assert private["host"] == {
        "https://mirror.example": {"capabilities": ["pull"]},
        "https://172.17.0.1:5443": {"capabilities": ["pull", "resolve", "push"]},
    }


def test_explicit_routes_override_automatic_mirror(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(local_infra, "_CONTAINERD_CERTS_DIR", str(tmp_path))
    direct = ContainerdRegistryConfig(server="https://registry-1.docker.io")
    routes = _local_registry_routes(LocalPortRegistrySetting(port=5002), None, {"docker.io": direct})
    assert routes == {"docker.io": direct}
    commands = _containerd_registry_commands(routes)
    assert not any("getent" in command for command in commands)
    subprocess.run(["sh", "-eu"], input="systemctl() { :; }\n" + "\n".join(commands), text=True, check=True)
    assert tomllib.loads((tmp_path / "docker.io/hosts.toml").read_text()) == {"server": direct.server}
    assert _containerd_registry_commands({}) == []
