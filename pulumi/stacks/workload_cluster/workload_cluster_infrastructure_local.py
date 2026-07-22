"""Local workload-cluster infrastructure for ``local`` instances.

Produces a local CAPD-backed workload-cluster infrastructure resource graph
for the requested instance:

1. On the management cluster (via ``pulumi-runner`` SA): explicit CAPI
    ``Cluster`` / ``DockerCluster`` / ``KubeadmControlPlane`` /
    ``MachineDeployment`` resources. CAPI then provisions the instance's
    workload k8s cluster on the docker infrastructure provider.
2. On the resulting workload cluster (via a second k8s provider built
    from the ``${cluster}-kubeconfig`` Secret CAPI publishes on the management
    cluster): local-path storage.

State backend
-------------
This component currently runs inside the PKO-owned ``ca4s-init`` stack, so its
resources live in the init stack's shared ``file:///state`` state. Separate
tenants/workload stack boundaries can be reintroduced later if isolated state is
needed.
"""

from __future__ import annotations

import base64
import os
import re
import urllib.request
from typing import Any, Mapping

import pulumi
import pulumi_kubernetes as k8s
import pulumi_local as local
import yaml

from lib.config import NonEmptyStr, PulumiConfigModel, StrictPositiveInt
from stacks.kubernetes_annotations import (
    foreground_delete_annotations,
    pulumi_wait_for,
)
from stacks.workload_cluster.registry_setting import RegistryConfig
from stacks.workload_cluster.workload_cluster_infrastructure import (
    AUTOSCALER_MAX_ANNOTATION,
    AUTOSCALER_MIN_ANNOTATION,
    ClusterAPIAutoscaler,
    ClusterAPIAutoscalerOutputs,
    CONTROLLER_NODE_TYPE,
    NODE_TYPE_LABEL,
    controller_bootstrap_tolerations,
    controller_node_affinity,
    controller_node_selector,
    controller_taint,
    controller_tolerations,
    machine_deployment_labels,
    worker_labels,
)
from stacks.workload_cluster.workload_cluster_storage import LocalPathStorage


_CAPI_API_VERSION = "cluster.x-k8s.io/v1beta2"
_BOOTSTRAP_API_VERSION = "bootstrap.cluster.x-k8s.io/v1beta2"
_CONTROL_PLANE_API_VERSION = "controlplane.cluster.x-k8s.io/v1beta2"
_INFRASTRUCTURE_API_VERSION = "infrastructure.cluster.x-k8s.io/v1beta2"

_NAMESPACE = "default"
_KUBERNETES_VERSION = "v1.36.1"
_POD_CIDR = "192.168.0.0/16"
_SERVICE_CIDR = "10.128.0.0/12"
_SERVICE_DOMAIN = "cluster.local"

_WORKLOAD_KUBECONFIG_SECRET_KEY = "value"

_CALICO_CHART_REPO = "https://docs.tigera.io/calico/charts"
_CALICO_CHART_NAME = "tigera-operator"
_CALICO_CHART_VERSION = "v3.32.0"
_CALICO_OPERATOR_CRDS_URL = (
    "https://raw.githubusercontent.com/projectcalico/calico/"
    f"{_CALICO_CHART_VERSION}/manifests/operator-crds.yaml"
)
_CALICO_OPERATOR_NAMESPACE = "tigera-operator"

_DNS_LABEL_MAX_LENGTH = 63
_DNS_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9]+")
_WAIT_FOR_CONTROL_PLANE_AVAILABLE = "condition=ControlPlaneReady"
_SERVICE_ACCOUNT_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SERVICE_ACCOUNT_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

_DOCKER_IO_HOSTS_DIR = "/etc/containerd/certs.d/docker.io"
_DOCKER_IO_SERVER = "https://registry-1.docker.io"
_DOCKER_HUB_PUBLIC_MIRROR = "https://mirror.gcr.io"
_DOCKER_DESKTOP_HOST = "host.docker.internal"


class LocalMachineDeploymentSpec(PulumiConfigModel):
    name: NonEmptyStr
    node_type: NonEmptyStr
    replicas: StrictPositiveInt
    autoscaler_bounds: tuple[StrictPositiveInt, StrictPositiveInt] | None = None


def _autoscaler_annotations(
    bounds: tuple[int, int] | None,
) -> dict[str, str]:
    if bounds is None:
        return {}
    min_replicas, max_replicas = bounds
    return {
        AUTOSCALER_MIN_ANNOTATION: str(min_replicas),
        AUTOSCALER_MAX_ANNOTATION: str(max_replicas),
    }


def _containerd_docker_io_mirror_commands(
    registry_setting: RegistryConfig | None,
) -> list[str]:
    if registry_setting is None:
        hosts_toml = (
            f'server = "{_DOCKER_IO_SERVER}"\n\n'
            f'[host."{_DOCKER_HUB_PUBLIC_MIRROR}"]\n'
            '  capabilities = ["pull", "resolve"]\n'
        )
        return [
            f"mkdir -p {_DOCKER_IO_HOSTS_DIR}",
            f"cat >{_DOCKER_IO_HOSTS_DIR}/hosts.toml <<'EOF'\n{hosts_toml}EOF",
            "systemctl restart containerd",
        ]

    return [
        f"mkdir -p {_DOCKER_IO_HOSTS_DIR}",
        (
            f"_CA4S_REGISTRY_HOST={_DOCKER_DESKTOP_HOST}\n"
            'if ! getent hosts "${_CA4S_REGISTRY_HOST}" >/dev/null 2>&1; then\n'
            "  _CA4S_REGISTRY_HOST=$(ip route show default "
            "| awk '{print $3; exit}')\n"
            "fi\n"
            'if [ -z "${_CA4S_REGISTRY_HOST}" ]; then\n'
            '  echo "could not determine local registry host" >&2\n'
            "  exit 1\n"
            "fi\n"
            f"cat >{_DOCKER_IO_HOSTS_DIR}/hosts.toml <<EOF\n"
            f'server = "{_DOCKER_IO_SERVER}"\n\n'
            f'[host."http://${{_CA4S_REGISTRY_HOST}}:{registry_setting.port}"]\n'
            '  capabilities = ["pull", "resolve"]\n'
            "EOF"
        ),
        "systemctl restart containerd",
    ]


def _resource_name(tenant: str, suffix: str) -> str:
    normalized = _DNS_LABEL_INVALID_CHARS.sub("-", tenant.lower()).strip("-")
    if not normalized:
        raise ValueError("tenant must contain at least one alphanumeric character")
    normalized = normalized[: _DNS_LABEL_MAX_LENGTH - len(suffix) - 1].rstrip("-")
    return f"{normalized}-{suffix}"


def _health_check() -> dict[str, object]:
    return {
        "checks": {
            "unhealthyNodeConditions": [
                {"type": "Ready", "status": "Unknown", "timeoutSeconds": 300},
                {"type": "Ready", "status": "False", "timeoutSeconds": 300},
            ],
        },
    }


def _kubelet_extra_args(node_type: str | None = None) -> list[dict[str, str]]:
    args = [
        {
            "name": "eviction-hard",
            "value": "nodefs.available<0%,nodefs.inodesFree<0%,imagefs.available<0%",
        }
    ]
    if node_type is not None:
        args.append(
            {"name": "node-labels", "value": f"{NODE_TYPE_LABEL}={node_type}"}
        )
    return args


def _node_registration(
    node_type: str | None = None,
) -> dict[str, object]:
    registration: dict[str, object] = {
        "kubeletExtraArgs": _kubelet_extra_args(node_type),
    }
    if node_type == CONTROLLER_NODE_TYPE:
        registration["taints"] = [controller_taint()]
    return registration


def _docker_machine_template(
    name: str,
    resource_name: str,
    *,
    custom_image: str,
    opts: pulumi.ResourceOptions | None = None,
) -> k8s.apiextensions.CustomResource:
    return k8s.apiextensions.CustomResource(
        resource_name,
        api_version=_INFRASTRUCTURE_API_VERSION,
        kind="DockerMachineTemplate",
        metadata={"name": name, "namespace": _NAMESPACE},
        spec={
            "template": {
                "spec": {
                    "customImage": custom_image,
                    "extraMounts": [
                        {
                            "containerPath": "/var/run/docker.sock",
                            "hostPath": "/var/run/docker.sock",
                        }
                    ],
                },
            },
        },
        opts=opts,
    )


def _kubeadm_config_template(
    name: str,
    resource_name: str,
    *,
    pre_kubeadm_commands: list[str],
    node_type: str | None = None,
    opts: pulumi.ResourceOptions | None = None,
) -> k8s.apiextensions.CustomResource:
    return k8s.apiextensions.CustomResource(
        resource_name,
        api_version=_BOOTSTRAP_API_VERSION,
        kind="KubeadmConfigTemplate",
        metadata={"name": name, "namespace": _NAMESPACE},
        spec={
            "template": {
                "spec": {
                    "preKubeadmCommands": pre_kubeadm_commands,
                    "joinConfiguration": {
                        "nodeRegistration": _node_registration(node_type),
                    },
                },
            },
        },
        opts=opts,
    )


def _api_group(api_version: str) -> str:
    return api_version.split("/", 1)[0]


def _object_ref(api_version: str, kind: str, name: str) -> dict[str, str]:
    return {"apiGroup": _api_group(api_version), "kind": kind, "name": name}


def _calico_values() -> dict[str, object]:
    return {
        "nodeSelector": controller_node_selector(),
        "affinity": controller_node_affinity(),
        "tolerations": controller_bootstrap_tolerations(),
        "installation": {
            "controlPlaneNodeSelector": controller_node_selector(),
            "controlPlaneTolerations": controller_tolerations(),
            "calicoNetwork": {
                "ipPools": [
                    {
                        "name": "default-ipv4-ippool",
                        "blockSize": 26,
                        "cidr": _POD_CIDR,
                        "encapsulation": "VXLANCrossSubnet",
                        "natOutgoing": "Enabled",
                        "nodeSelector": "all()",
                    }
                ],
            },
        },
    }


def _read_url(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8")


def _calico_operator_crd_dependencies(
    calico_operator_crds: k8s.yaml.ConfigGroup,
) -> list[pulumi.Input[pulumi.Resource]]:
    return [
        calico_operator_crds.get_resource(
            "apiextensions.k8s.io/v1/CustomResourceDefinition",
            name,
        )
        for name in (
            "apiservers.operator.tigera.io",
            "goldmanes.operator.tigera.io",
            "installations.operator.tigera.io",
            "whiskers.operator.tigera.io",
        )
    ]


class CalicoCNI(pulumi.ComponentResource):
    """Calico CNI for local CAPD workload clusters."""

    chart_version: pulumi.Output[str]
    status: pulumi.Output[Any]

    def __init__(
        self,
        name: str,
        *,
        provider: k8s.Provider,
        depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:CalicoCNI",
            name,
            props={},
            opts=opts,
        )

        def child_options(
            *,
            depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
        ) -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
                retain_on_delete=True,
            )

        namespace = k8s.core.v1.Namespace(
            "namespace",
            metadata={
                "name": _CALICO_OPERATOR_NAMESPACE,
                "labels": {"pod-security.kubernetes.io/enforce": "privileged"},
            },
            opts=child_options(depends_on=depends_on),
        )
        operator_crds = k8s.yaml.ConfigGroup(
            "operator-crds",
            yaml=[_read_url(_CALICO_OPERATOR_CRDS_URL)],
            opts=child_options(depends_on=[namespace]),
        )
        operator = k8s.helm.v3.Release(
            "operator",
            chart=_CALICO_CHART_NAME,
            version=_CALICO_CHART_VERSION,
            repository_opts={"repo": _CALICO_CHART_REPO},
            namespace=_CALICO_OPERATOR_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            skip_crds=True,
            timeout=600,
            values=_calico_values(),
            opts=child_options(
                depends_on=[
                    namespace,
                    *_calico_operator_crd_dependencies(operator_crds),
                ],
            ),
        )

        self.chart_version = pulumi.Output.from_input(_CALICO_CHART_VERSION)
        self.status = operator.status
        self.register_outputs(
            {
                "chart_version": self.chart_version,
                "status": self.status,
            }
        )


class WorkerClass(pulumi.ComponentResource):
    machine_deployment_name: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        cluster_name: str,
        node_image: str,
        pre_kubeadm_commands: list[str],
        worker: LocalMachineDeploymentSpec,
        provider: k8s.Provider,
        cluster: pulumi.Resource,
        control_plane: pulumi.Resource,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:workload:WorkerClass", name, props={}, opts=opts)

        machine_template_name = _resource_name(instance, f"{worker.name}-machine")
        bootstrap_template_name = _resource_name(instance, f"{worker.name}-bootstrap")
        machine_deployment_name = _resource_name(instance, worker.name)
        autoscaler_annotations = _autoscaler_annotations(worker.autoscaler_bounds)
        labels = worker_labels(cluster_name, worker.node_type)
        machine_deployment_label_set = machine_deployment_labels(
            cluster_name,
            worker.node_type,
            autoscaler_enabled=worker.autoscaler_bounds is not None,
        )

        def child_options(
            *,
            depends_on: list[pulumi.Resource] | None = None,
            ignore_changes: list[str] | None = None,
        ) -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
                ignore_changes=ignore_changes,
            )

        machine_template = _docker_machine_template(
            machine_template_name,
            f"cluster-{worker.name}-machine-template",
            custom_image=node_image,
            opts=child_options(),
        )
        bootstrap_template = _kubeadm_config_template(
            bootstrap_template_name,
            f"cluster-{worker.name}-bootstrap-template",
            pre_kubeadm_commands=pre_kubeadm_commands,
            node_type=worker.node_type,
            opts=child_options(),
        )

        machine_deployment_spec: dict[str, Any] = {
            "clusterName": cluster_name,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {
                    "labels": labels,
                    **(
                        {"annotations": autoscaler_annotations}
                        if autoscaler_annotations
                        else {}
                    ),
                },
                "spec": {
                    "clusterName": cluster_name,
                    "version": _KUBERNETES_VERSION,
                    "deletion": {"nodeDeletionTimeoutSeconds": 10},
                    "bootstrap": {
                        "configRef": _object_ref(
                            _BOOTSTRAP_API_VERSION,
                            "KubeadmConfigTemplate",
                            bootstrap_template_name,
                        ),
                    },
                    "infrastructureRef": _object_ref(
                        _INFRASTRUCTURE_API_VERSION,
                        "DockerMachineTemplate",
                        machine_template_name,
                    ),
                },
            },
        }
        machine_deployment_spec["replicas"] = worker.replicas

        machine_deployment = k8s.apiextensions.CustomResource(
            f"cluster-{worker.name}-machine-deployment",
            api_version=_CAPI_API_VERSION,
            kind="MachineDeployment",
            metadata={
                "name": machine_deployment_name,
                "namespace": _NAMESPACE,
                "labels": machine_deployment_label_set,
                "annotations": foreground_delete_annotations(autoscaler_annotations),
            },
            spec=machine_deployment_spec,
            opts=child_options(
                depends_on=[
                    cluster,
                    control_plane,
                    machine_template,
                    bootstrap_template,
                ],
                ignore_changes=(
                    ["spec.replicas"] if worker.autoscaler_bounds is not None else None
                ),
            ),
        )

        k8s.apiextensions.CustomResource(
            f"cluster-{worker.name}-health-check",
            api_version=_CAPI_API_VERSION,
            kind="MachineHealthCheck",
            metadata={
                "name": _resource_name(instance, f"{worker.name}-health"),
                "namespace": _NAMESPACE,
            },
            spec={
                "clusterName": cluster_name,
                "selector": {"matchLabels": labels},
                **_health_check(),
            },
            opts=child_options(depends_on=[machine_deployment]),
        )

        self.machine_deployment_name = pulumi.Output.from_input(
            machine_deployment_name
        )
        self.register_outputs(
            {"machine_deployment_name": self.machine_deployment_name}
        )


class ManagementKubeconfig(pulumi.ComponentResource):
    kubeconfig: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:ManagementKubeconfig",
            name,
            props={},
            opts=opts,
        )

        self.kubeconfig = self._from_service_account()

        self.register_outputs({"kubeconfig": self.kubeconfig})

    def _from_service_account(self) -> pulumi.Output[str]:
        if "KUBERNETES_SERVICE_HOST" not in os.environ:
            raise RuntimeError(
                "tenants/workload components must run inside the PKO workspace pod"
            )

        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        server = f"https://{host}:{port}"
        token_file = local.get_sensitive_file_output(
            filename=_SERVICE_ACCOUNT_TOKEN_PATH,
            opts=pulumi.InvokeOutputOptions(parent=self),
        )
        ca_file = local.get_file_output(
            filename=_SERVICE_ACCOUNT_CA_PATH,
            opts=pulumi.InvokeOutputOptions(parent=self),
        )

        def build(args: list[str]) -> str:
            token, ca = args
            ca_data = base64.b64encode(ca.encode("utf-8")).decode("ascii")
            return yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "clusters": [
                        {
                            "name": "management",
                            "cluster": {
                                "server": server,
                                "certificate-authority-data": ca_data,
                            },
                        }
                    ],
                    "contexts": [
                        {
                            "name": "management",
                            "context": {
                                "cluster": "management",
                                "user": "pulumi-runner",
                            },
                        }
                    ],
                    "current-context": "management",
                    "users": [
                        {
                            "name": "pulumi-runner",
                            "user": {"token": token.strip()},
                        }
                    ],
                },
                sort_keys=False,
            )

        return pulumi.Output.secret(
            pulumi.Output.all(token_file.content, ca_file.content).apply(build)
        )


def _decode_secret_data_value(data: Mapping[str, str], key: str) -> str:
    encoded_value = data.get(key)
    if not encoded_value:
        raise KeyError(f"Secret data[{key!r}] is missing")
    return base64.b64decode(encoded_value).decode("utf-8")


class LocalWorkloadClusterInfrastructure(pulumi.ComponentResource):
    """Environment-specific Kubernetes workload-cluster bring-up.

    For the local class this means management-cluster CAPI/CAPD resources plus
    the provider/kubeconfig needed to talk to the resulting workload cluster.
    """

    cluster_name: pulumi.Output[str]
    docker_cluster_name: pulumi.Output[str]
    control_plane_name: pulumi.Output[str]
    worker_machine_deployments: list[pulumi.Output[str]]
    workload_kubeconfig: pulumi.Output[str]
    workload_provider: k8s.Provider
    workload_kubeconfig_secret: k8s.core.v1.Secret
    cluster_control_plane_available: k8s.apiextensions.CustomResourcePatch
    calico_operator_chart_version: pulumi.Output[str]
    calico_operator_status: pulumi.Output[Any]
    cluster_autoscaler: pulumi.Output[ClusterAPIAutoscalerOutputs] | None

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        worker_machine_deployments: tuple[LocalMachineDeploymentSpec, ...],
        registry: RegistryConfig | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:LocalWorkloadClusterInfrastructure",
            name,
            props={},
            opts=opts,
        )

        cluster_name = _resource_name(instance, "workload")
        node_image = f"kindest/node:{_KUBERNETES_VERSION}"
        pre_kubeadm_commands = _containerd_docker_io_mirror_commands(registry)

        def child_options(
            *,
            provider: pulumi.ProviderResource | None = None,
            depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
        ) -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
            )

        management_kubeconfig = ManagementKubeconfig(
            "management-kubeconfig",
            opts=child_options(),
        )
        management_provider = k8s.Provider(
            "management-k8s",
            kubeconfig=management_kubeconfig.kubeconfig,
            delete_unreachable=False,
            upsert_existing_objects=True,
            opts=child_options(depends_on=[management_kubeconfig]),
        )

        control_plane_template_name = _resource_name(instance, "control-plane")
        control_plane_machine_template = _docker_machine_template(
            control_plane_template_name,
            "cluster-control-plane-machine-template",
            custom_image=node_image,
            opts=child_options(provider=management_provider),
        )

        cluster = k8s.apiextensions.CustomResource(
            "cluster",
            api_version=_CAPI_API_VERSION,
            kind="Cluster",
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                "annotations": foreground_delete_annotations(),
            },
            spec={
                "clusterNetwork": {
                    "pods": {"cidrBlocks": [_POD_CIDR]},
                    "services": {"cidrBlocks": [_SERVICE_CIDR]},
                    "serviceDomain": _SERVICE_DOMAIN,
                },
                "controlPlaneRef": _object_ref(
                    _CONTROL_PLANE_API_VERSION,
                    "KubeadmControlPlane",
                    control_plane_template_name,
                ),
                "infrastructureRef": _object_ref(
                    _INFRASTRUCTURE_API_VERSION,
                    "DockerCluster",
                    cluster_name,
                ),
            },
            opts=child_options(provider=management_provider),
        )

        docker_cluster = k8s.apiextensions.CustomResource(
            "cluster-docker-cluster",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind="DockerCluster",
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                "annotations": foreground_delete_annotations(
                    pulumi_wait_for("condition=Ready")
                ),
            },
            spec={},
            opts=child_options(
                provider=management_provider,
                depends_on=[cluster],
            ),
        )

        # We intentionally do not use ClusterClass/topology here. Topology hides
        # the concrete DockerCluster/KubeadmControlPlane/MachineDeployment
        # resources from Pulumi, so Kubernetes starts deleting the DockerCluster
        # sibling while the final control-plane DockerMachine may still need the
        # CAPD load balancer. In CAPD v1.11.1 that can strand the DockerMachine
        # finalizer after the load balancer is gone. ClusterClass mostly removes
        # boilerplate that Pulumi is already good at generating, while costing
        # us the ordering surface we need for this local provider's brittle
        # finalization path.
        kubeadm_control_plane = k8s.apiextensions.CustomResource(
            "cluster-control-plane",
            api_version=_CONTROL_PLANE_API_VERSION,
            kind="KubeadmControlPlane",
            metadata={
                "name": control_plane_template_name,
                "namespace": _NAMESPACE,
                "annotations": foreground_delete_annotations(
                    pulumi_wait_for("condition=Initialized")
                ),
            },
            spec={
                "replicas": 1,
                "version": _KUBERNETES_VERSION,
                "machineTemplate": {
                    "spec": {
                        "infrastructureRef": _object_ref(
                            _INFRASTRUCTURE_API_VERSION,
                            "DockerMachineTemplate",
                            control_plane_template_name,
                        ),
                        "deletion": {"nodeDeletionTimeoutSeconds": 10},
                    },
                },
                "kubeadmConfigSpec": {
                    "clusterConfiguration": {
                        "apiServer": {
                            "certSANs": [
                                "localhost",
                                "127.0.0.1",
                                "0.0.0.0",
                                "host.docker.internal",
                            ],
                        },
                    },
                    "initConfiguration": {
                        "nodeRegistration": _node_registration(),
                    },
                    "joinConfiguration": {
                        "nodeRegistration": _node_registration(),
                    },
                    "preKubeadmCommands": pre_kubeadm_commands,
                },
            },
            opts=child_options(
                provider=management_provider,
                depends_on=[cluster, docker_cluster, control_plane_machine_template],
            ),
        )

        cluster_control_plane_available = k8s.apiextensions.CustomResourcePatch(
            "cluster-control-plane-available",
            api_version=_CAPI_API_VERSION,
            kind="Cluster",
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                "annotations": pulumi_wait_for(_WAIT_FOR_CONTROL_PLANE_AVAILABLE),
            },
            opts=child_options(
                provider=management_provider,
                depends_on=[kubeadm_control_plane],
            ),
        )

        k8s.apiextensions.CustomResource(
            "cluster-control-plane-health-check",
            api_version=_CAPI_API_VERSION,
            kind="MachineHealthCheck",
            metadata={
                "name": _resource_name(instance, "control-plane-health"),
                "namespace": _NAMESPACE,
            },
            spec={
                "clusterName": cluster_name,
                "selector": {
                    "matchLabels": {"cluster.x-k8s.io/control-plane": ""},
                },
                **_health_check(),
            },
            opts=child_options(
                provider=management_provider,
                depends_on=[kubeadm_control_plane],
            ),
        )

        worker_machine_deployment_names: list[pulumi.Output[str]] = []
        worker_classes: list[WorkerClass] = []
        for worker in worker_machine_deployments:
            worker_class = WorkerClass(
                f"cluster-{worker.name}",
                instance=instance,
                cluster_name=cluster_name,
                node_image=node_image,
                pre_kubeadm_commands=pre_kubeadm_commands,
                worker=worker,
                provider=management_provider,
                cluster=cluster,
                control_plane=kubeadm_control_plane,
                opts=child_options(),
            )
            worker_classes.append(worker_class)
            worker_machine_deployment_names.append(worker_class.machine_deployment_name)

        workload_kubeconfig_secret = k8s.core.v1.Secret.get(
            "workload-kubeconfig-secret",
            id=f"{_NAMESPACE}/{cluster_name}-kubeconfig",
            opts=child_options(
                provider=management_provider,
                depends_on=[cluster_control_plane_available],
            ),
        )
        workload_kubeconfig = pulumi.Output.secret(
            workload_kubeconfig_secret.data.apply(
                lambda data: _decode_secret_data_value(
                    data,
                    _WORKLOAD_KUBECONFIG_SECRET_KEY,
                )
            )
        )
        workload_provider = k8s.Provider(
            "workload-k8s",
            kubeconfig=workload_kubeconfig,
            delete_unreachable=True,
            upsert_existing_objects=True,
            opts=child_options(depends_on=[workload_kubeconfig_secret]),
        )

        calico_cni = CalicoCNI(
            "calico-cni",
            provider=workload_provider,
            depends_on=[workload_kubeconfig_secret],
            opts=child_options(provider=workload_provider),
        )

        local_path_storage = LocalPathStorage(
            "local-path-storage",
            provider=workload_provider,
            depends_on=[workload_kubeconfig_secret, calico_cni],
            opts=child_options(provider=workload_provider),
        )

        cluster_autoscaler_enabled = any(
            worker.autoscaler_bounds is not None for worker in worker_machine_deployments
        )
        cluster_autoscaler: ClusterAPIAutoscaler | None = None
        if cluster_autoscaler_enabled:
            cluster_autoscaler = ClusterAPIAutoscaler(
                "cluster-autoscaler",
                instance=instance,
                cluster_name=cluster_name,
                workload_kubeconfig=workload_kubeconfig,
                provider=management_provider,
                depends_on=[workload_kubeconfig_secret, *worker_classes],
                opts=child_options(provider=management_provider),
            )

        self.cluster_name = pulumi.Output.from_input(cluster_name)
        self.docker_cluster_name = pulumi.Output.from_input(cluster_name)
        self.control_plane_name = pulumi.Output.from_input(control_plane_template_name)
        self.worker_machine_deployments = worker_machine_deployment_names
        self.workload_kubeconfig = workload_kubeconfig
        self.workload_provider = workload_provider
        self.workload_kubeconfig_secret = workload_kubeconfig_secret
        self.cluster_control_plane_available = cluster_control_plane_available
        self.calico_operator_chart_version = calico_cni.chart_version
        self.calico_operator_status = calico_cni.status
        self.cluster_autoscaler = (
            cluster_autoscaler.outputs if cluster_autoscaler is not None else None
        )

        self.register_outputs(
            {
                "cluster_name": self.cluster_name,
                "docker_cluster_name": self.docker_cluster_name,
                "control_plane_name": self.control_plane_name,
                "worker_machine_deployments": self.worker_machine_deployments,
                "calico_operator_chart_version": self.calico_operator_chart_version,
                "calico_operator_status": self.calico_operator_status,
                "local_path_storage_class_name": local_path_storage.storage_class_name,
                "cluster_autoscaler": (
                    self.cluster_autoscaler.apply(lambda outputs: outputs.model_dump())
                    if self.cluster_autoscaler is not None
                    else None
                ),
            }
        )
