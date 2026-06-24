"""Kubernetes-flavored workload-cluster inventory and fan-out."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping

import pulumi

from lib.outputs import to_output_value
from stacks.workload_cluster.workload_cluster_class_aks import AKSWorkloadClusterClass
from stacks.workload_cluster.workload_cluster_class_local import LocalWorkloadClusterClass


_PROJECT_NAME = "ca4s-workload-cluster"
SPEC_CONFIG_KEY = "spec"
_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_WORKLOAD_CLUSTER_CLASSES = {
    "aks": AKSWorkloadClusterClass,
    "local": LocalWorkloadClusterClass,
}

_CONFIG_WORKLOAD_CLUSTERS = "workloadClusters"
_CONFIG_METADATA = "metadata"
_CONFIG_NAME = "name"
_CONFIG_LABELS = "labels"
_CONFIG_ANNOTATIONS = "annotations"
_CONFIG_CLASS_NAME = "className"
_CONFIG_PARAMETERS = "parameters"


@dataclass(frozen=True)
class ObjectMeta:
    name: str
    labels: Mapping[str, str] = field(default_factory=dict)
    annotations: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkloadClusterSpec:
    metadata: ObjectMeta
    class_name: str
    parameters: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TenantsSpec:
    workload_clusters: tuple[WorkloadClusterSpec, ...]


@dataclass(frozen=True)
class WorkloadClusterContext:
    subscription_id: str | None = None
    identity_name: pulumi.Input[str] | None = None
    identity_namespace: pulumi.Input[str] | None = None


_DEFAULT_TENANTS_SPEC = TenantsSpec(
    workload_clusters=(
        WorkloadClusterSpec(
            metadata=ObjectMeta(name="local"),
            class_name="local",
        ),
    ),
)


def _validate_dns_label(kind: str, value: str) -> None:
    if not value or len(value) > 63 or not _DNS_LABEL.fullmatch(value):
        raise ValueError(f"workload cluster {kind} {value!r} must be a DNS label")


def _require_mapping(field_path: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_path} must be an object")
    return value


def _require_non_empty_str(field_path: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_path} must be a non-empty string")
    return value


def _require_str_map(field_path: str, value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_path} must be an object of strings")
    parsed: dict[str, str] = {}
    for key, item_value in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field_path} keys must be non-empty strings")
        if not isinstance(item_value, str):
            raise ValueError(f"{field_path}.{key} must be a string")
        parsed[key] = item_value
    return parsed


def _parse_object_meta(field_path: str, value: object) -> ObjectMeta:
    metadata = _require_mapping(field_path, value)
    name = _require_non_empty_str(f"{field_path}.{_CONFIG_NAME}", metadata.get(_CONFIG_NAME))
    _validate_dns_label("metadata.name", name)

    labels_value = metadata.get(_CONFIG_LABELS)
    labels = (
        {}
        if labels_value is None
        else _require_str_map(f"{field_path}.{_CONFIG_LABELS}", labels_value)
    )
    annotations_value = metadata.get(_CONFIG_ANNOTATIONS)
    annotations = (
        {}
        if annotations_value is None
        else _require_str_map(
            f"{field_path}.{_CONFIG_ANNOTATIONS}", annotations_value
        )
    )
    return ObjectMeta(name=name, labels=labels, annotations=annotations)


def _parse_workload_cluster_spec(index: int, value: object) -> WorkloadClusterSpec:
    field_path = f"{SPEC_CONFIG_KEY}.{_CONFIG_WORKLOAD_CLUSTERS}[{index}]"
    item = _require_mapping(field_path, value)
    metadata = _parse_object_meta(
        f"{field_path}.{_CONFIG_METADATA}", item.get(_CONFIG_METADATA)
    )

    spec = _require_mapping(f"{field_path}.{SPEC_CONFIG_KEY}", item.get(SPEC_CONFIG_KEY))
    class_name = _require_non_empty_str(
        f"{field_path}.{SPEC_CONFIG_KEY}.{_CONFIG_CLASS_NAME}",
        spec.get(_CONFIG_CLASS_NAME),
    )
    _validate_dns_label("spec.className", class_name)

    parameters_value = spec.get(_CONFIG_PARAMETERS)
    parameters = (
        {}
        if parameters_value is None
        else dict(
            _require_mapping(
                f"{field_path}.{SPEC_CONFIG_KEY}.{_CONFIG_PARAMETERS}",
                parameters_value,
            )
        )
    )
    return WorkloadClusterSpec(
        metadata=metadata,
        class_name=class_name,
        parameters=parameters,
    )


def parse_tenants_spec(value: object | None) -> TenantsSpec:
    """Parse a Kubernetes-flavored workload-cluster inventory.

    Expected input shape:

        {"workloadClusters": [{"metadata": {"name": "local"}, "spec": {"className": "local"}}]}
    """
    if value is None:
        return _DEFAULT_TENANTS_SPEC
    if isinstance(value, TenantsSpec):
        return value

    spec = _require_mapping(SPEC_CONFIG_KEY, value)
    workload_clusters_value = spec.get(_CONFIG_WORKLOAD_CLUSTERS)
    if not isinstance(workload_clusters_value, list):
        raise ValueError(f"{SPEC_CONFIG_KEY}.{_CONFIG_WORKLOAD_CLUSTERS} must be a list")

    workload_clusters = tuple(
        _parse_workload_cluster_spec(index, item)
        for index, item in enumerate(workload_clusters_value)
    )
    if not workload_clusters:
        raise ValueError(
            f"{SPEC_CONFIG_KEY}.{_CONFIG_WORKLOAD_CLUSTERS} must not be empty"
        )

    names = [workload_cluster.metadata.name for workload_cluster in workload_clusters]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(
            f"{SPEC_CONFIG_KEY}.{_CONFIG_WORKLOAD_CLUSTERS} contains duplicate names: "
            + ", ".join(duplicates)
        )

    return TenantsSpec(
        workload_clusters=tuple(
            sorted(workload_clusters, key=lambda item: item.metadata.name)
        )
    )


def _workload_cluster_output(cluster: Any) -> dict[str, Any]:
    return {
        name: to_output_value(value)
        for name, value in vars(cluster).items()
        if not name.startswith("_")
    }


class Tenants(pulumi.ComponentResource):
    """Instantiate workload-cluster instances from ``spec.workloadClusters``."""

    workload_clusters: list[dict[str, Any]]

    def __init__(
        self,
        name: str,
        *,
        spec: TenantsSpec | Mapping[str, object] | None = None,
        context: WorkloadClusterContext | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:workload:Tenants", name, props={}, opts=opts)

        workload_clusters_spec = parse_tenants_spec(
            spec
            if spec is not None
            else pulumi.Config(_PROJECT_NAME).get_object(SPEC_CONFIG_KEY)
        )
        workload_context = context or WorkloadClusterContext()

        child_clusters = [
            self._instantiate_workload_cluster(workload_cluster, workload_context)
            for workload_cluster in workload_clusters_spec.workload_clusters
        ]

        self.workload_clusters = [
            _workload_cluster_output(cluster) for cluster in child_clusters
        ]

        self.register_outputs({"workload_clusters": self.workload_clusters})

    def _instantiate_workload_cluster(
        self,
        workload_cluster: WorkloadClusterSpec,
        context: WorkloadClusterContext,
    ) -> Any:
        try:
            workload_cluster_class = _WORKLOAD_CLUSTER_CLASSES[workload_cluster.class_name]
        except KeyError:
            raise ValueError(
                f"unsupported workload cluster class {workload_cluster.class_name!r} "
                f"for instance {workload_cluster.metadata.name!r}; supported classes: "
                + ", ".join(sorted(_WORKLOAD_CLUSTER_CLASSES))
            ) from None

        return workload_cluster_class(
            f"{workload_cluster.metadata.name}-workload-cluster",
            instance=workload_cluster.metadata.name,
            parameters=workload_cluster.parameters,
            context=context,
            opts=pulumi.ResourceOptions(parent=self),
        )