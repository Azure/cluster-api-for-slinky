from __future__ import annotations

import pytest

from stacks.kubernetes_annotations import (
    DELETE_PROPAGATION_FOREGROUND,
    PULUMI_PATCH_FORCE_ANNOTATION,
    PULUMI_DELETION_PROPAGATION_POLICY_ANNOTATION,
    PULUMI_WAIT_FOR_ANNOTATION,
    foreground_delete_annotations,
    pulumi_patch_force,
    pulumi_wait_for,
)


def test_pulumi_wait_for_uses_raw_single_expression() -> None:
    assert pulumi_wait_for("condition=Ready") == {
        PULUMI_WAIT_FOR_ANNOTATION: "condition=Ready",
    }


def test_pulumi_wait_for_json_encodes_multiple_expressions() -> None:
    assert pulumi_wait_for("jsonpath={.foo}", "condition=Bar") == {
        PULUMI_WAIT_FOR_ANNOTATION: '["jsonpath={.foo}", "condition=Bar"]',
    }


def test_pulumi_wait_for_requires_at_least_one_expression() -> None:
    with pytest.raises(ValueError, match="at least one wait expression"):
        pulumi_wait_for()


def test_pulumi_patch_force_sets_true_annotation() -> None:
    assert pulumi_patch_force() == {PULUMI_PATCH_FORCE_ANNOTATION: "true"}


def test_foreground_delete_annotations_preserve_existing_annotations() -> None:
    annotations = foreground_delete_annotations(
        pulumi_wait_for("condition=Ready")
    )

    assert annotations == {
        PULUMI_WAIT_FOR_ANNOTATION: "condition=Ready",
        PULUMI_DELETION_PROPAGATION_POLICY_ANNOTATION: (
            DELETE_PROPAGATION_FOREGROUND
        ),
    }