"""Tenant aggregate for the ``local`` outer env.

The workload-cluster project stack name is the outer env, ``local``. The
top-level ``__main__.py`` imports this module and instantiates
``TenantLocal``. This component reads ``spec.workloadClusters`` config, fans
out one child per entry, handles cross-workload-cluster concerns, and
instantiates the selected ``workload_cluster_local_<class>.py`` component for
each instance.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import re
from typing import Any, Mapping

import pulumi


_PROJECT_NAME = "ca4s-workload-cluster"
_SPEC_CONFIG_KEY = "spec"
_CLASS_MODULE_PREFIX = "workload_cluster_local_"
_CLASS_EXPORT = "WorkloadClusterClass"
_MODULE_INVALID_CHARS = re.compile(r"[^a-z0-9_]+")
_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")


@dataclass(frozen=True)
class WorkloadClusterSpec:
    name: str
    cluster_class: str


@dataclass(frozen=True)
class TenantLocalSpec:
    workload_clusters: tuple[WorkloadClusterSpec, ...]


_DEFAULT_TENANT_LOCAL_SPEC = TenantLocalSpec(
    workload_clusters=(
        WorkloadClusterSpec(name="local", cluster_class="local"),
    ),
)


def _validate_dns_label(kind: str, value: str) -> None:
    if not value or len(value) > 63 or not _DNS_LABEL.fullmatch(value):
        raise ValueError(
            f"local workload cluster {kind} {value!r} must be a DNS label"
        )


def _parse_workload_cluster_spec(value: object) -> WorkloadClusterSpec:
    if not isinstance(value, Mapping):
        raise ValueError("local workload cluster entries must be objects")

    name = value.get("name")
    cluster_class = value.get("class")
    if not isinstance(name, str):
        raise ValueError("local workload cluster name must be a string")
    if not isinstance(cluster_class, str):
        raise ValueError("local workload cluster class must be a string")

    _validate_dns_label("instance name", name)
    _validate_dns_label("class", cluster_class)
    return WorkloadClusterSpec(name=name, cluster_class=cluster_class)


def parse_tenant_local_spec(
    value: object | None,
) -> TenantLocalSpec:
    """Parse the local workload-cluster inventory shape.

    Expected input shape:

        {"workloadClusters": [{"name": "local", "class": "local"}]}
    """
    if value is None:
        return _DEFAULT_TENANT_LOCAL_SPEC
    if isinstance(value, TenantLocalSpec):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("local workload clusters spec must be an object")

    workload_clusters_value = value.get("workloadClusters")
    if not isinstance(workload_clusters_value, list):
        raise ValueError("local workload clusters spec workloadClusters must be a list")

    workload_clusters = tuple(
        _parse_workload_cluster_spec(item) for item in workload_clusters_value
    )
    if not workload_clusters:
        raise ValueError("local workload clusters spec workloadClusters must not be empty")

    names = [workload_cluster.name for workload_cluster in workload_clusters]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(
            "local workload clusters spec workloadClusters contains duplicate names: "
            + ", ".join(duplicates)
        )

    return TenantLocalSpec(
        workload_clusters=tuple(sorted(workload_clusters, key=lambda item: item.name))
    )


def _module_suffix(value: str, *, field_name: str) -> str:
    suffix = _MODULE_INVALID_CHARS.sub("_", value.lower()).strip("_")
    if not suffix:
        raise ValueError(f"{field_name} must contain at least one module-safe character")
    if suffix[0].isdigit():
        suffix = f"class_{suffix}"
    return suffix


class TenantLocal(pulumi.ComponentResource):
    """Instantiate local workload-cluster instances from ``workloadClusters``."""

    workload_clusters: list[dict[str, Any]]
    cluster_classes: list[pulumi.Output[str]]
    cluster_instances: list[pulumi.Output[str]]
    cluster_names: list[pulumi.Output[str]]
    docker_cluster_names: list[pulumi.Output[str]]
    control_plane_names: list[pulumi.Output[str]]
    worker_machine_deployments: list[list[pulumi.Output[str]]]
    calico_operator_chart_versions: list[pulumi.Output[str]]
    calico_operator_statuses: list[pulumi.Output[Any]]
    workload_cluster_readiness: list[pulumi.Output[bool]]
    todos: list[pulumi.Output[str]]

    def __init__(
        self,
        name: str,
        *,
        spec: TenantLocalSpec | Mapping[str, object] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:TenantLocal",
            name,
            props={},
            opts=opts,
        )

        workload_clusters_spec = parse_tenant_local_spec(
            spec
            if spec is not None
            else pulumi.Config(_PROJECT_NAME).get_object(_SPEC_CONFIG_KEY)
        )

        child_clusters = [
            self._instantiate_workload_cluster(workload_cluster)
            for workload_cluster in workload_clusters_spec.workload_clusters
        ]

        self.cluster_classes = [cluster.cluster_class for cluster in child_clusters]
        self.cluster_instances = [cluster.cluster_instance for cluster in child_clusters]
        self.cluster_names = [cluster.cluster_name for cluster in child_clusters]
        self.docker_cluster_names = [
            cluster.docker_cluster_name for cluster in child_clusters
        ]
        self.control_plane_names = [
            cluster.control_plane_name for cluster in child_clusters
        ]
        self.worker_machine_deployments = [
            cluster.worker_machine_deployments for cluster in child_clusters
        ]
        self.calico_operator_chart_versions = [
            cluster.calico_operator_chart_version for cluster in child_clusters
        ]
        self.calico_operator_statuses = [
            cluster.calico_operator_status for cluster in child_clusters
        ]
        self.workload_cluster_readiness = [
            cluster.workload_cluster_ready for cluster in child_clusters
        ]
        self.todos = [cluster.todo for cluster in child_clusters]
        self.workload_clusters = [
            {
                "class": cluster.cluster_class,
                "instance": cluster.cluster_instance,
                "cluster_name": cluster.cluster_name,
                "docker_cluster_name": cluster.docker_cluster_name,
                "control_plane_name": cluster.control_plane_name,
                "worker_machine_deployments": cluster.worker_machine_deployments,
                "calico_operator_chart_version": cluster.calico_operator_chart_version,
                "calico_operator_status": cluster.calico_operator_status,
                "workload_cluster_ready": cluster.workload_cluster_ready,
                "todo": cluster.todo,
            }
            for cluster in child_clusters
        ]

        self.register_outputs(
            {
                "workload_clusters": self.workload_clusters,
                "cluster_classes": self.cluster_classes,
                "cluster_instances": self.cluster_instances,
                "cluster_names": self.cluster_names,
                "docker_cluster_names": self.docker_cluster_names,
                "control_plane_names": self.control_plane_names,
                "worker_machine_deployments": self.worker_machine_deployments,
                "calico_operator_chart_versions": self.calico_operator_chart_versions,
                "calico_operator_statuses": self.calico_operator_statuses,
                "workload_cluster_readiness": self.workload_cluster_readiness,
                "todos": self.todos,
            }
        )

    def _instantiate_workload_cluster(
        self,
        workload_cluster: WorkloadClusterSpec,
    ) -> Any:
        class_module_suffix = _module_suffix(
            workload_cluster.cluster_class,
            field_name="workload cluster class",
        )
        module_name = f"{_CLASS_MODULE_PREFIX}{class_module_suffix}"
        package_module_name = (
            f"{__package__}.{module_name}" if __package__ else module_name
        )

        try:
            module = importlib.import_module(package_module_name)
        except ModuleNotFoundError as exc:
            if exc.name != package_module_name:
                raise
            raise ValueError(
                f"unsupported local workload cluster class {workload_cluster.cluster_class!r} "
                f"for instance {workload_cluster.name!r}: expected sibling module "
                f"{module_name!r}. "
                f"Create pulumi/stacks/workload_cluster/{module_name}.py exposing "
                f"a ``{_CLASS_EXPORT}`` ComponentResource class to register this class."
            ) from None

        try:
            workload_cluster_class = getattr(module, _CLASS_EXPORT)
        except AttributeError:
            raise ValueError(
                f"local workload cluster class module {module_name!r} must expose "
                f"a ``{_CLASS_EXPORT}`` ComponentResource class."
            ) from None

        return workload_cluster_class(
            f"{workload_cluster.name}-workload-cluster",
            instance=workload_cluster.name,
            opts=pulumi.ResourceOptions(parent=self),
        )
