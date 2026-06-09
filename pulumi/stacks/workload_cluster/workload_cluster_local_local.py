"""Local workload-cluster class for ``local`` instances.

Selected by ``tenant_local.py`` for ``workloadClusters`` entries with
``class: local``. Produces a local CAPD-backed workload-cluster resource graph
for the requested instance:

1. On the management cluster (via ``pulumi-runner`` SA): explicit CAPI
    ``Cluster`` / ``DockerCluster`` / ``KubeadmControlPlane`` /
    ``MachineDeployment`` resources. CAPI then provisions the instance's
    workload k8s cluster on the docker infrastructure provider.
2. On the resulting workload cluster (via a second k8s provider built
   from the ``${cluster}-kubeconfig`` Secret CAPI publishes on the management
    cluster): Calico. Slinky CRDs, ``slurm-operator``, the Slurm chart, and
    per-instance ``NodeSet``s belong here too, but are still TODO.

State backend
-------------
This component currently runs inside the PKO-owned ``ca4s-init`` stack, so its
resources live in the init stack's shared ``file:///state`` state. A separate
tenant/workload stack boundary can be reintroduced later if isolated state is
needed.
"""

from __future__ import annotations

import base64
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

import pulumi
import pulumi_kubernetes as k8s
import pulumi_local as local
import yaml

try:
    from .registry_setting import (
        REGISTRY_CONFIG_NAME,
        RegistrySetting,
        parse_registry_setting,
    )
except ImportError:
    from registry_setting import (
        REGISTRY_CONFIG_NAME,
        RegistrySetting,
        parse_registry_setting,
    )


_CAPI_API_VERSION = "cluster.x-k8s.io/v1beta2"
_BOOTSTRAP_API_VERSION = "bootstrap.cluster.x-k8s.io/v1beta2"
_CONTROL_PLANE_API_VERSION = "controlplane.cluster.x-k8s.io/v1beta2"
_INFRASTRUCTURE_API_VERSION = "infrastructure.cluster.x-k8s.io/v1beta2"

_NAMESPACE = "default"
_KUBERNETES_VERSION = "v1.36.1"
_POD_CIDR = "192.168.0.0/16"
_SERVICE_CIDR = "10.128.0.0/12"
_SERVICE_DOMAIN = "cluster.local"

_CALICO_CHART_REPO = "https://docs.tigera.io/calico/charts"
_CALICO_CHART_NAME = "tigera-operator"
_CALICO_CHART_VERSION = "v3.32.0"
_CALICO_OPERATOR_CRDS_URL = (
    "https://raw.githubusercontent.com/projectcalico/calico/"
    f"{_CALICO_CHART_VERSION}/manifests/operator-crds.yaml"
)
_CALICO_OPERATOR_NAMESPACE = "tigera-operator"
_WORKLOAD_KUBECONFIG_SECRET_KEY = "value"

_NODE_TYPE_LABEL = "slinky.slurm.net/node-type"
_CONTROLLER_NODE_TYPE = "controller"
_COMPUTE_NODE_TYPE = "compute"
_CLUSTER_CLASS = "local"
_AUTOSCALER_MIN_ANNOTATION = (
    "cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size"
)
_AUTOSCALER_MAX_ANNOTATION = (
    "cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size"
)
_DNS_LABEL_MAX_LENGTH = 63
_DNS_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9]+")
_WAIT_FOR_ANNOTATION = "pulumi.com/waitFor"
_WAIT_FOR_CONTROL_PLANE_AVAILABLE = "condition=ControlPlaneAvailable"
_SERVICE_ACCOUNT_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SERVICE_ACCOUNT_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

_DOCKER_IO_HOSTS_DIR = "/etc/containerd/certs.d/docker.io"
_DOCKER_IO_SERVER = "https://registry-1.docker.io"
_DOCKER_HUB_PUBLIC_MIRROR = "https://mirror.gcr.io"
_DOCKER_DESKTOP_HOST = "host.docker.internal"


@dataclass(frozen=True)
class WorkerClassSpec:
    name: str
    node_type: str
    replicas: int | None = 1
    controller: bool = False
    annotations: dict[str, str] = field(default_factory=dict)


_WORKER_NODE_CLASSES = (
    WorkerClassSpec(
        name="head",
        node_type=_CONTROLLER_NODE_TYPE,
        controller=True,
    ),
    WorkerClassSpec(
        name="compute",
        node_type=_COMPUTE_NODE_TYPE,
        replicas=None,
        annotations={
            _AUTOSCALER_MIN_ANNOTATION: "1",
            _AUTOSCALER_MAX_ANNOTATION: "10",
        },
    ),
)


def _read_registry_setting() -> RegistrySetting | None:
    return parse_registry_setting(
        pulumi.Config("ca4s-workload-cluster").get_object(REGISTRY_CONFIG_NAME)
    )


def _containerd_docker_io_mirror_commands(
    registry_setting: RegistrySetting | None,
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

    port = registry_setting["port"]
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
            f'[host."http://${{_CA4S_REGISTRY_HOST}}:{port}"]\n'
            '  capabilities = ["pull", "resolve"]\n'
            "EOF"
        ),
        "systemctl restart containerd",
    ]


def _resource_name(tenant: str, suffix: str) -> str:
    normalized = _DNS_LABEL_INVALID_CHARS.sub("-", tenant.lower()).strip("-")
    if not normalized:
        raise ValueError("tenant must contain at least one alphanumeric character")
    max_tenant_length = _DNS_LABEL_MAX_LENGTH - len(suffix) - 1
    normalized = normalized[:max_tenant_length].rstrip("-")
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


def _kubelet_extra_args() -> list[dict[str, str]]:
    return [
        {
            "name": "eviction-hard",
            "value": "nodefs.available<0%,nodefs.inodesFree<0%,imagefs.available<0%",
        }
    ]


def _node_registration(controller: bool = False) -> dict[str, object]:
    node_registration: dict[str, object] = {
        "kubeletExtraArgs": _kubelet_extra_args(),
    }
    if controller:
        node_registration["taints"] = [
            {"key": "slinky.slurm.net/controller", "effect": "NoSchedule"}
        ]
    return node_registration


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
    controller: bool = False,
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
                        "nodeRegistration": _node_registration(controller),
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


def _worker_labels(cluster_name: str, worker: WorkerClassSpec) -> dict[str, str]:
    return {
        "cluster.x-k8s.io/cluster-name": cluster_name,
        _NODE_TYPE_LABEL: worker.node_type,
    }


def _calico_values() -> dict[str, object]:
    return {
        "installation": {
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
        worker: WorkerClassSpec,
        provider: k8s.Provider,
        cluster: pulumi.Resource,
        control_plane: pulumi.Resource,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:workload:WorkerClass", name, props={}, opts=opts)

        machine_template_name = _resource_name(instance, f"{worker.name}-machine")
        bootstrap_template_name = _resource_name(instance, f"{worker.name}-bootstrap")
        machine_deployment_name = _resource_name(instance, worker.name)
        labels = _worker_labels(cluster_name, worker)

        def child_options(
            *, depends_on: list[pulumi.Resource] | None = None
        ) -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
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
            controller=worker.controller,
            opts=child_options(),
        )

        machine_deployment_spec: dict[str, Any] = {
            "clusterName": cluster_name,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {
                    "labels": labels,
                    **(
                        {"annotations": worker.annotations}
                        if worker.annotations
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
        if worker.replicas is not None:
            machine_deployment_spec["replicas"] = worker.replicas

        machine_deployment = k8s.apiextensions.CustomResource(
            f"cluster-{worker.name}-machine-deployment",
            api_version=_CAPI_API_VERSION,
            kind="MachineDeployment",
            metadata={
                "name": machine_deployment_name,
                "namespace": _NAMESPACE,
                **({"annotations": worker.annotations} if worker.annotations else {}),
            },
            spec=machine_deployment_spec,
            opts=child_options(
                depends_on=[
                    cluster,
                    control_plane,
                    machine_template,
                    bootstrap_template,
                ]
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
                "tenant/workload components must run inside the PKO workspace pod"
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


class LocalWorkloadClusterClass(pulumi.ComponentResource):
    """Reusable local workload-cluster class.

    A class captures the resource graph shape. The ``instance`` passed to
    the constructor supplies the concrete identity used for Kubernetes object
    names and Pulumi outputs.
    """

    cluster_class: pulumi.Output[str]
    cluster_instance: pulumi.Output[str]
    cluster_name: pulumi.Output[str]
    docker_cluster_name: pulumi.Output[str]
    control_plane_name: pulumi.Output[str]
    worker_machine_deployments: list[pulumi.Output[str]]
    calico_operator_chart_version: pulumi.Output[str]
    calico_operator_status: pulumi.Output[Any]
    workload_cluster_ready: pulumi.Output[bool]
    todo: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        worker_node_classes: tuple[WorkerClassSpec, ...] = _WORKER_NODE_CLASSES,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Build one local workload-cluster instance from this class."""
        super().__init__(
            "ca4s:workload:LocalWorkloadClusterClass",
            name,
            props={},
            opts=opts,
        )

        cluster_name = _resource_name(instance, "workload")
        node_image = f"kindest/node:{_KUBERNETES_VERSION}"
        pre_kubeadm_commands = _containerd_docker_io_mirror_commands(
            _read_registry_setting()
        )

        def child_options(
            *,
            provider: pulumi.ProviderResource | None = None,
            depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
            retain_on_delete: bool | None = None,
        ) -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
                retain_on_delete=retain_on_delete,
            )

        management_kubeconfig = ManagementKubeconfig(
            "management-kubeconfig",
            opts=child_options(),
        )
        management_provider = k8s.Provider(
            "management-k8s",
            kubeconfig=management_kubeconfig.kubeconfig,
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
                "annotations": {_WAIT_FOR_ANNOTATION: "condition=Ready"},
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
                "annotations": {_WAIT_FOR_ANNOTATION: "condition=Initialized"},
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
                "annotations": {
                    _WAIT_FOR_ANNOTATION: _WAIT_FOR_CONTROL_PLANE_AVAILABLE,
                },
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
        for worker in worker_node_classes:
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
            opts=child_options(depends_on=[workload_kubeconfig_secret]),
        )

        calico_namespace = k8s.core.v1.Namespace(
            "calico-operator-namespace",
            metadata={
                "name": _CALICO_OPERATOR_NAMESPACE,
                "labels": {"pod-security.kubernetes.io/enforce": "privileged"},
            },
            opts=child_options(
                provider=workload_provider,
                # Keep CNI alive while CAPI deletes the workload cluster. The
                # whole workload cluster is disposable, so retained Calico
                # resources are removed when CAPD deletes the cluster containers.
                retain_on_delete=True,
            ),
        )
        calico_operator_crds = k8s.yaml.ConfigGroup(
            "calico-operator-crds",
            yaml=[_read_url(_CALICO_OPERATOR_CRDS_URL)],
            opts=child_options(
                provider=workload_provider,
                depends_on=[calico_namespace],
                retain_on_delete=True,
            ),
        )
        calico_operator = k8s.helm.v3.Release(
            "calico-operator",
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
                provider=workload_provider,
                depends_on=[
                    calico_namespace,
                    *_calico_operator_crd_dependencies(calico_operator_crds),
                ],
                retain_on_delete=True,
            ),
        )

        self.cluster_class = pulumi.Output.from_input(_CLUSTER_CLASS)
        self.cluster_instance = pulumi.Output.from_input(instance)
        self.cluster_name = pulumi.Output.from_input(cluster_name)
        self.docker_cluster_name = pulumi.Output.from_input(cluster_name)
        self.control_plane_name = pulumi.Output.from_input(control_plane_template_name)
        self.worker_machine_deployments = worker_machine_deployment_names
        self.calico_operator_chart_version = pulumi.Output.from_input(
            _CALICO_CHART_VERSION
        )
        self.calico_operator_status = calico_operator.status
        self.workload_cluster_ready = pulumi.Output.from_input(False)
        self.todo = pulumi.Output.from_input(
            "Install slurm-operator and NodeSets on the Calico-enabled workload cluster."
        )

        self.register_outputs(
            {
                "cluster_class": self.cluster_class,
                "cluster_instance": self.cluster_instance,
                "cluster_name": self.cluster_name,
                "docker_cluster_name": self.docker_cluster_name,
                "control_plane_name": self.control_plane_name,
                "worker_machine_deployments": self.worker_machine_deployments,
                "calico_operator_chart_version": self.calico_operator_chart_version,
                "calico_operator_status": self.calico_operator_status,
                "workload_cluster_ready": self.workload_cluster_ready,
                "todo": self.todo,
            }
        )

WorkloadClusterClass = LocalWorkloadClusterClass
