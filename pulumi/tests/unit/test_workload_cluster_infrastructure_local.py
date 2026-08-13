from __future__ import annotations

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
    _WAIT_FOR_CONTROL_PLANE_AVAILABLE,
    _node_registration,
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
