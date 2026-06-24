from __future__ import annotations

from stacks.workload_cluster.workload_cluster_infrastructure import (
    AUTOSCALER_MAX_ANNOTATION,
    AUTOSCALER_MIN_ANNOTATION,
)
from stacks.workload_cluster.workload_cluster_infrastructure_local import (
    _DELETE_FOREGROUND,
    _DELETION_PROPAGATION_ANNOTATION,
    _foreground_delete_annotations,
)


def test_foreground_delete_annotations_preserve_existing_annotations() -> None:
    annotations = _foreground_delete_annotations(
        {
            AUTOSCALER_MIN_ANNOTATION: "1",
            AUTOSCALER_MAX_ANNOTATION: "10",
        }
    )

    assert annotations == {
        AUTOSCALER_MIN_ANNOTATION: "1",
        AUTOSCALER_MAX_ANNOTATION: "10",
        _DELETION_PROPAGATION_ANNOTATION: _DELETE_FOREGROUND,
    }
