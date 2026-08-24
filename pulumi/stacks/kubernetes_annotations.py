"""Shared Pulumi Kubernetes annotation helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping

PULUMI_SKIP_AWAIT_ANNOTATION = "pulumi.com/skipAwait"
PULUMI_WAIT_FOR_ANNOTATION = "pulumi.com/waitFor"
PULUMI_PATCH_FORCE_ANNOTATION = "pulumi.com/patchForce"
PULUMI_DELETION_PROPAGATION_POLICY_ANNOTATION = (
    "pulumi.com/deletionPropagationPolicy"
)

DELETE_PROPAGATION_FOREGROUND = "Foreground"
DELETE_PROPAGATION_ORPHAN = "Orphan"


def pulumi_wait_for(*expressions: str) -> dict[str, str]:
    if not expressions:
        raise ValueError("at least one wait expression is required")
    if len(expressions) == 1:
        return {PULUMI_WAIT_FOR_ANNOTATION: expressions[0]}
    return {PULUMI_WAIT_FOR_ANNOTATION: json.dumps(list(expressions))}


def pulumi_patch_force() -> dict[str, str]:
    return {PULUMI_PATCH_FORCE_ANNOTATION: "true"}


def foreground_delete_annotations(
    annotations: Mapping[str, str] | None = None,
) -> dict[str, str]:
    return {
        **(dict(annotations) if annotations else {}),
        PULUMI_DELETION_PROPAGATION_POLICY_ANNOTATION: DELETE_PROPAGATION_FOREGROUND,
    }
