from __future__ import annotations

from stacks.kubernetes_annotations import (
    DELETE_PROPAGATION_FOREGROUND,
    PULUMI_DELETION_PROPAGATION_POLICY_ANNOTATION,
    foreground_delete_annotations,
)
from stacks.workload_cluster.workload_cluster_infrastructure import (
    AUTOSCALER_MAX_ANNOTATION,
    AUTOSCALER_MIN_ANNOTATION,
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
