"""Per-tenant body for the ``local-example`` workload-cluster stack.

Selected by ``workload_cluster_local.py`` when the workload-cluster stack name
is ``local-example``. Produces, for that env/tenant pair:

1. On the management cluster (via ``pulumi-runner`` SA): explicit CAPI
    ``Cluster`` / ``DockerCluster`` / ``KubeadmControlPlane`` /
    ``MachineDeployment`` resources. CAPI then provisions the tenant's
    workload k8s cluster on the docker infrastructure provider.
2. On the resulting workload cluster (via a second k8s provider built
   from the ``${cluster}-kubeconfig`` Secret CAPI publishes on the management
   cluster): Calico. Slinky CRDs, ``slurm-operator``, the Slurm chart, and
   per-tenant ``NodeSet``s belong here too, but are still TODO.

State backend
-------------
Shared ``file:///state`` PVC, same as the other two inner stacks.
Each per-tenant stack instance gets its own subdirectory under the
PVC keyed by Pulumi's ``<org>/<project>/<stack>`` naming —
``organization/ca4s-workload-cluster/local-<tenant>/`` — so tenant
state is isolated even though the backend is shared.
"""

from __future__ import annotations

import base64
import os
import re
import ssl
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import pulumi
import pulumi.dynamic as dynamic
import pulumi_kubernetes as k8s
import pulumi_local as local
import yaml


_CAPI_API_VERSION = "cluster.x-k8s.io/v1beta2"
_BOOTSTRAP_API_VERSION = "bootstrap.cluster.x-k8s.io/v1beta2"
_CONTROL_PLANE_API_VERSION = "controlplane.cluster.x-k8s.io/v1beta2"
_INFRASTRUCTURE_API_VERSION = "infrastructure.cluster.x-k8s.io/v1beta2"

_NAMESPACE = "default"
_KUBERNETES_VERSION = "v1.34.0"
_POD_CIDR = "192.168.0.0/16"
_SERVICE_CIDR = "10.128.0.0/12"
_SERVICE_DOMAIN = "cluster.local"

_CALICO_CHART_REPO = "https://docs.tigera.io/calico/charts"
_CALICO_CHART_NAME = "tigera-operator"
_CALICO_CHART_VERSION = "v3.30.3"
_CALICO_OPERATOR_NAMESPACE = "tigera-operator"
_WORKLOAD_KUBECONFIG_SECRET_KEY = "value"
_WORKLOAD_KUBECONFIG_TIMEOUT_SECONDS = 30 * 60

_NODE_TYPE_LABEL = "slinky.slurm.net/node-type"
_CONTROLLER_NODE_TYPE = "controller"
_COMPUTE_NODE_TYPE = "compute"
_TENANT = "example"
_AUTOSCALER_MIN_ANNOTATION = (
    "cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size"
)
_AUTOSCALER_MAX_ANNOTATION = (
    "cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size"
)
_DNS_LABEL_MAX_LENGTH = 63
_DNS_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9]+")
_WAIT_FOR_ANNOTATION = "pulumi.com/waitFor"
_SERVICE_ACCOUNT_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SERVICE_ACCOUNT_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

_CONTAINERD_DOCKER_IO_MIRROR_COMMANDS = [
    "mkdir -p /etc/containerd/certs.d/docker.io",
    'cat >/etc/containerd/certs.d/docker.io/hosts.toml <<\'EOF\'\nserver = "https://registry-1.docker.io"\n\n[host."https://mirror.gcr.io"]\n  capabilities = ["pull", "resolve"]\nEOF',
    "systemctl restart containerd",
]


@dataclass(frozen=True)
class WorkerNodeClass:
    name: str
    node_type: str
    replicas: int | None = 1
    controller: bool = False
    annotations: dict[str, str] = field(default_factory=dict)


_WORKER_NODE_CLASSES = (
    WorkerNodeClass(
        name="head",
        node_type=_CONTROLLER_NODE_TYPE,
        controller=True,
    ),
    WorkerNodeClass(
        name="compute",
        node_type=_COMPUTE_NODE_TYPE,
        replicas=None,
        annotations={
            _AUTOSCALER_MIN_ANNOTATION: "1",
            _AUTOSCALER_MAX_ANNOTATION: "10",
        },
    ),
)


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
                    "preKubeadmCommands": _CONTAINERD_DOCKER_IO_MIRROR_COMMANDS,
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


def _worker_labels(cluster_name: str, worker: WorkerNodeClass) -> dict[str, str]:
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
                "workload-cluster stacks must run inside the PKO workspace pod"
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


def _decode_secret_data_value(data: dict[str, str], key: str) -> str:
    encoded_value = data.get(key)
    if not encoded_value:
        raise KeyError(f"Secret data[{key!r}] is missing")
    return base64.b64decode(encoded_value).decode("utf-8")


def _current_kubeconfig_cluster_and_user(
    kubeconfig: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    parsed = yaml.safe_load(kubeconfig)
    current_context_name = parsed["current-context"]
    context = next(
        context_entry["context"]
        for context_entry in parsed["contexts"]
        if context_entry["name"] == current_context_name
    )
    cluster = next(
        cluster_entry["cluster"]
        for cluster_entry in parsed["clusters"]
        if cluster_entry["name"] == context["cluster"]
    )
    user = next(
        user_entry["user"]
        for user_entry in parsed["users"]
        if user_entry["name"] == context["user"]
    )
    return cluster, user


def _write_kubeconfig_pem_file(content_b64: str) -> tempfile.NamedTemporaryFile[str]:
    handle = tempfile.NamedTemporaryFile("w", delete=True)
    handle.write(base64.b64decode(content_b64).decode("utf-8"))
    handle.flush()
    return handle


def _wait_for_workload_api(kubeconfig: str, deadline: float) -> None:
    cluster, user = _current_kubeconfig_cluster_and_user(kubeconfig)
    server = cluster["server"].rstrip("/")

    with (
        _write_kubeconfig_pem_file(cluster["certificate-authority-data"]) as ca_file,
        _write_kubeconfig_pem_file(user["client-certificate-data"]) as cert_file,
        _write_kubeconfig_pem_file(user["client-key-data"]) as key_file,
    ):
        context = ssl.create_default_context(cafile=ca_file.name)
        context.load_cert_chain(certfile=cert_file.name, keyfile=key_file.name)
        while True:
            if _workload_api_endpoint_ready(
                server, context, "/readyz"
            ) and _workload_api_endpoint_ready(
                server, context, "/openapi/v2?timeout=32s"
            ):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out waiting for workload API {server} readiness/openapi"
                )
            time.sleep(5)


def _workload_api_endpoint_ready(
    server: str, context: ssl.SSLContext, path: str
) -> bool:
    request = urllib.request.Request(f"{server}{path}")
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ssl.SSLError):
        return False


class _WorkloadApiReadyProvider(dynamic.ResourceProvider):
    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        deadline = time.monotonic() + int(props["timeout_seconds"])
        _wait_for_workload_api(props["kubeconfig"], deadline)
        return dynamic.CreateResult(id_=props["id"], outs=props)

    def diff(
        self,
        id_: str,
        olds: dict[str, Any],
        news: dict[str, Any],
    ) -> dynamic.DiffResult:
        replaces = [
            key for key in ("id", "kubeconfig") if olds.get(key) != news.get(key)
        ]
        return dynamic.DiffResult(
            changes=bool(replaces),
            replaces=replaces,
        )

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        deadline = time.monotonic() + int(props["timeout_seconds"])
        _wait_for_workload_api(props["kubeconfig"], deadline)
        return dynamic.ReadResult(id_=id_, outs=props)


class WorkloadApiReady(dynamic.Resource):
    def __init__(
        self,
        name: str,
        *,
        kubeconfig: pulumi.Input[str],
        timeout_seconds: pulumi.Input[int] = _WORKLOAD_KUBECONFIG_TIMEOUT_SECONDS,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            _WorkloadApiReadyProvider(),
            name,
            {
                "id": name,
                "timeout_seconds": timeout_seconds,
                "kubeconfig": kubeconfig,
            },
            opts,
        )


def run() -> None:
    """Build the local/example workload-cluster resource graph.

    The management-cluster side declares a local CAPD ``Cluster`` plus
    explicit control-plane and worker resources: one Slurm head node and one
    compute node group. Once CAPI publishes the workload kubeconfig Secret,
    the workload-cluster side installs Calico.

    Still TODO for this tenant:
        * Install ``slurm-operator-crds`` + ``slurm-operator`` (mirroring
          ``slurm-operator-values.yaml``) + the Slurm chart + NodeSets
          (mirroring ``slurm-cluster.yaml``) on the workload cluster.
    """
    cluster_name = _resource_name(_TENANT, "workload")
    node_image = f"kindest/node:{_KUBERNETES_VERSION}"
    management_kubeconfig = ManagementKubeconfig("management-kubeconfig")
    management_provider = k8s.Provider(
        "management-k8s",
        kubeconfig=management_kubeconfig.kubeconfig,
        opts=pulumi.ResourceOptions(depends_on=[management_kubeconfig]),
    )

    control_plane_template_name = _resource_name(_TENANT, "control-plane")
    control_plane_machine_template = _docker_machine_template(
        control_plane_template_name,
        "cluster-control-plane-machine-template",
        custom_image=node_image,
        opts=pulumi.ResourceOptions(provider=management_provider),
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
        opts=pulumi.ResourceOptions(provider=management_provider),
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
        opts=pulumi.ResourceOptions(provider=management_provider, depends_on=[cluster]),
    )

    # We intentionally do not use ClusterClass/topology here. Topology hides the
    # concrete DockerCluster/KubeadmControlPlane/MachineDeployment resources from
    # Pulumi, so Kubernetes starts deleting the DockerCluster sibling while the
    # final control-plane DockerMachine may still need the CAPD load balancer.
    # In CAPD v1.11.1 that can strand the DockerMachine finalizer after the load
    # balancer is gone. ClusterClass mostly removes boilerplate that Pulumi is
    # already good at generating, while costing us the ordering surface we need
    # for this local provider's brittle finalization path.
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
                "preKubeadmCommands": _CONTAINERD_DOCKER_IO_MIRROR_COMMANDS,
            },
        },
        opts=pulumi.ResourceOptions(
            provider=management_provider,
            depends_on=[cluster, docker_cluster, control_plane_machine_template],
        ),
    )

    control_plane_health_check = k8s.apiextensions.CustomResource(
        "cluster-control-plane-health-check",
        api_version=_CAPI_API_VERSION,
        kind="MachineHealthCheck",
        metadata={
            "name": _resource_name(_TENANT, "control-plane-health"),
            "namespace": _NAMESPACE,
        },
        spec={
            "clusterName": cluster_name,
            "selector": {
                "matchLabels": {"cluster.x-k8s.io/control-plane": ""},
            },
            **_health_check(),
        },
        opts=pulumi.ResourceOptions(
            provider=management_provider,
            depends_on=[kubeadm_control_plane],
        ),
    )

    worker_machine_deployments: list[k8s.apiextensions.CustomResource] = []
    worker_health_checks: list[k8s.apiextensions.CustomResource] = []
    for worker in _WORKER_NODE_CLASSES:
        machine_template_name = _resource_name(_TENANT, f"{worker.name}-machine")
        bootstrap_template_name = _resource_name(_TENANT, f"{worker.name}-bootstrap")
        machine_deployment_name = _resource_name(_TENANT, worker.name)
        labels = _worker_labels(cluster_name, worker)

        machine_template = _docker_machine_template(
            machine_template_name,
            f"cluster-{worker.name}-machine-template",
            custom_image=node_image,
            opts=pulumi.ResourceOptions(provider=management_provider),
        )
        bootstrap_template = _kubeadm_config_template(
            bootstrap_template_name,
            f"cluster-{worker.name}-bootstrap-template",
            controller=worker.controller,
            opts=pulumi.ResourceOptions(provider=management_provider),
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
            opts=pulumi.ResourceOptions(
                provider=management_provider,
                depends_on=[
                    cluster,
                    kubeadm_control_plane,
                    machine_template,
                    bootstrap_template,
                ],
            ),
        )
        worker_machine_deployments.append(machine_deployment)

        worker_health_check = k8s.apiextensions.CustomResource(
            f"cluster-{worker.name}-health-check",
            api_version=_CAPI_API_VERSION,
            kind="MachineHealthCheck",
            metadata={
                "name": _resource_name(_TENANT, f"{worker.name}-health"),
                "namespace": _NAMESPACE,
            },
            spec={
                "clusterName": cluster_name,
                "selector": {"matchLabels": labels},
                **_health_check(),
            },
            opts=pulumi.ResourceOptions(
                provider=management_provider,
                depends_on=[machine_deployment],
            ),
        )
        worker_health_checks.append(worker_health_check)

    workload_kubeconfig_secret = k8s.core.v1.Secret.get(
        "workload-kubeconfig-secret",
        id=f"{_NAMESPACE}/{cluster_name}-kubeconfig",
        opts=pulumi.ResourceOptions(
            provider=management_provider,
            depends_on=[kubeadm_control_plane],
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
    workload_api_ready = WorkloadApiReady(
        "workload-api-ready",
        kubeconfig=workload_kubeconfig,
        opts=pulumi.ResourceOptions(depends_on=[workload_kubeconfig_secret]),
    )
    workload_provider = k8s.Provider(
        "workload-k8s",
        kubeconfig=workload_kubeconfig,
        opts=pulumi.ResourceOptions(depends_on=[workload_api_ready]),
    )

    calico_namespace = k8s.core.v1.Namespace(
        "calico-operator-namespace",
        metadata={
            "name": _CALICO_OPERATOR_NAMESPACE,
            "labels": {"pod-security.kubernetes.io/enforce": "privileged"},
        },
        opts=pulumi.ResourceOptions(
            provider=workload_provider,
            # Keep CNI alive while CAPI deletes the workload cluster. The whole
            # workload cluster is disposable, so retained Calico resources are
            # removed when CAPD deletes the cluster containers.
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
        timeout=600,
        values=_calico_values(),
        opts=pulumi.ResourceOptions(
            provider=workload_provider,
            depends_on=[calico_namespace],
            retain_on_delete=True,
        ),
    )

    # Echo the tenant + a marker so ``pulumi stack output`` confirms
    # the dispatcher routed correctly.
    pulumi.export("tenant", _TENANT)
    pulumi.export("cluster_name", cluster.metadata["name"])
    pulumi.export("docker_cluster_name", docker_cluster.metadata["name"])
    pulumi.export("control_plane_name", kubeadm_control_plane.metadata["name"])
    pulumi.export(
        "worker_machine_deployments",
        [
            machine_deployment.metadata["name"]
            for machine_deployment in worker_machine_deployments
        ],
    )
    pulumi.export("calico_operator_chart_version", _CALICO_CHART_VERSION)
    pulumi.export("calico_operator_status", calico_operator.status)
    pulumi.export("workload_cluster_ready", False)
    pulumi.export(
        "todo",
        "Install slurm-operator and NodeSets on the Calico-enabled workload cluster.",
    )
