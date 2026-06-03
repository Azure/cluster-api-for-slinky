"""Per-tenant body for the ``local-example`` workload-cluster stack.

Selected by ``workload_cluster_local.py`` when the workload-cluster stack name
is ``local-example``. Produces, for that env/tenant pair:

1. On the management cluster (via ``pulumi-runner`` SA): a CAPI
    ``ClusterClass`` + templates + ``Cluster`` with head/compute
    MachineDeployments. CAPI then provisions the tenant's workload k8s
    cluster on the docker infrastructure provider.
2. On the resulting workload cluster (via a second k8s provider built
   from the ``${cluster}-kubeconfig`` Secret CAPI publishes on mgmt):
   ``slurm-operator-crds`` + ``slurm-operator`` + the Slurm chart +
   per-tenant ``NodeSet``s (mirroring ``slurm-cluster.yaml`` /
   ``slurm-operator-values.yaml``). This is where the Slinky CRDs
   actually live — NOT on the management cluster.

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
import json
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pulumi
import pulumi.dynamic as dynamic
import pulumi_kubernetes as k8s


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
_SKIP_AWAIT_ANNOTATION = "pulumi.com/skipAwait"


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


def _docker_machine_template(
    name: str,
    resource_name: str,
) -> k8s.apiextensions.CustomResource:
    return k8s.apiextensions.CustomResource(
        resource_name,
        api_version=_INFRASTRUCTURE_API_VERSION,
        kind="DockerMachineTemplate",
        metadata={"name": name, "namespace": _NAMESPACE},
        spec={
            "template": {
                "spec": {
                    "extraMounts": [
                        {
                            "containerPath": "/var/run/docker.sock",
                            "hostPath": "/var/run/docker.sock",
                        }
                    ],
                },
            },
        },
    )


def _kubeadm_config_template(
    name: str,
    resource_name: str,
    *,
    controller: bool = False,
) -> k8s.apiextensions.CustomResource:
    node_registration: dict[str, object] = {
        "kubeletExtraArgs": _kubelet_extra_args(),
    }
    if controller:
        node_registration["taints"] = [
            {"key": "slinky.slurm.net/controller", "effect": "NoSchedule"}
        ]

    return k8s.apiextensions.CustomResource(
        resource_name,
        api_version=_BOOTSTRAP_API_VERSION,
        kind="KubeadmConfigTemplate",
        metadata={"name": name, "namespace": _NAMESPACE},
        spec={
            "template": {
                "spec": {
                    "joinConfiguration": {
                        "nodeRegistration": node_registration,
                    },
                },
            },
        },
    )


def _template_ref(api_version: str, kind: str, name: str) -> dict[str, str]:
    return {"apiVersion": api_version, "kind": kind, "name": name}


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


def _read_mgmt_secret(namespace: str, name: str) -> dict[str, Any] | None:
    if "KUBERNETES_SERVICE_HOST" in os.environ:
        return _read_mgmt_secret_in_cluster(namespace, name)
    return _read_mgmt_secret_with_kubectl(namespace, name)


def _read_mgmt_secret_in_cluster(namespace: str, name: str) -> dict[str, Any] | None:
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    with open(token_path, encoding="ascii") as token_file:
        token = token_file.read().strip()

    quoted_namespace = urllib.parse.quote(namespace, safe="")
    quoted_name = urllib.parse.quote(name, safe="")
    url = (
        f"https://{host}:{port}/api/v1/namespaces/"
        f"{quoted_namespace}/secrets/{quoted_name}"
    )
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )
    context = ssl.create_default_context(cafile=ca_path)
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _read_mgmt_secret_with_kubectl(namespace: str, name: str) -> dict[str, Any] | None:
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "secret", name, "-o", "json"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode == 0:
        return json.loads(result.stdout)
    if "NotFound" in result.stderr:
        return None
    raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def _wait_for_secret_value(
    *,
    namespace: str,
    name: str,
    key: str,
    timeout_seconds: int,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while True:
        secret = _read_mgmt_secret(namespace, name)
        if secret is not None:
            encoded_value = secret.get("data", {}).get(key)
            if encoded_value:
                return base64.b64decode(encoded_value).decode("utf-8")

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"timed out waiting for Secret {namespace}/{name} data[{key!r}]"
            )
        time.sleep(5)


class _KubeconfigSecretProvider(dynamic.ResourceProvider):
    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        kubeconfig = _wait_for_secret_value(
            namespace=props["namespace"],
            name=props["secret_name"],
            key=props["key"],
            timeout_seconds=int(props["timeout_seconds"]),
        )
        return dynamic.CreateResult(
            id_=f"{props['namespace']}/{props['secret_name']}",
            outs={**props, "kubeconfig": kubeconfig},
        )

    def diff(
        self,
        id_: str,
        olds: dict[str, Any],
        news: dict[str, Any],
    ) -> dynamic.DiffResult:
        replaces = [
            key
            for key in ("namespace", "secret_name", "key")
            if olds.get(key) != news.get(key)
        ]
        return dynamic.DiffResult(
            changes=bool(replaces),
            replaces=replaces,
            delete_before_replace=True,
        )

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        kubeconfig = _wait_for_secret_value(
            namespace=props["namespace"],
            name=props["secret_name"],
            key=props["key"],
            timeout_seconds=int(props["timeout_seconds"]),
        )
        return dynamic.ReadResult(id_=id_, outs={**props, "kubeconfig": kubeconfig})


class KubeconfigSecret(dynamic.Resource):
    kubeconfig: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        namespace: pulumi.Input[str],
        secret_name: pulumi.Input[str],
        key: pulumi.Input[str] = _WORKLOAD_KUBECONFIG_SECRET_KEY,
        timeout_seconds: pulumi.Input[int] = _WORKLOAD_KUBECONFIG_TIMEOUT_SECONDS,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        opts = pulumi.ResourceOptions.merge(
            opts,
            pulumi.ResourceOptions(additional_secret_outputs=["kubeconfig"]),
        )
        super().__init__(
            _KubeconfigSecretProvider(),
            name,
            {
                "namespace": namespace,
                "secret_name": secret_name,
                "key": key,
                "timeout_seconds": timeout_seconds,
                "kubeconfig": None,
            },
            opts,
        )
        self.kubeconfig = pulumi.Output.secret(self.kubeconfig)


def run() -> None:
    """Build the local/example workload-cluster resource graph.

    This first CAPI pass creates only the management-cluster side: a local
    CAPD ``ClusterClass`` plus templates, then one tenant ``Cluster`` using
    two MachineDeployments: one Slurm head node and one compute node group.
    It intentionally does not add the old SSH ``preKubeadmCommands`` from
    ``capi-quickstart.yaml``.

    Still TODO for this tenant:
            * Workload-cluster side: install ``slurm-operator-crds`` +
        ``slurm-operator`` (mirroring ``slurm-operator-values.yaml``)
        + the Slurm chart + NodeSets (mirroring ``slurm-cluster.yaml``).
    """
    cluster_name = _resource_name(_TENANT, "workload")
    cluster_class_name = _resource_name(_TENANT, "capd")

    docker_cluster_template_name = _resource_name(_TENANT, "docker-cluster")
    control_plane_template_name = _resource_name(_TENANT, "control-plane")
    head_machine_template_name = _resource_name(_TENANT, "head-machine")
    compute_machine_template_name = _resource_name(_TENANT, "compute-machine")
    head_bootstrap_template_name = _resource_name(_TENANT, "head-bootstrap")
    compute_bootstrap_template_name = _resource_name(_TENANT, "compute-bootstrap")

    docker_cluster_template = k8s.apiextensions.CustomResource(
        "cluster-docker-cluster-template",
        api_version=_INFRASTRUCTURE_API_VERSION,
        kind="DockerClusterTemplate",
        metadata={"name": docker_cluster_template_name, "namespace": _NAMESPACE},
        spec={"template": {"spec": {}}},
    )

    control_plane_template = k8s.apiextensions.CustomResource(
        "cluster-control-plane-template",
        api_version=_CONTROL_PLANE_API_VERSION,
        kind="KubeadmControlPlaneTemplate",
        metadata={"name": control_plane_template_name, "namespace": _NAMESPACE},
        spec={
            "template": {
                "spec": {
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
                            "nodeRegistration": {
                                "kubeletExtraArgs": _kubelet_extra_args(),
                            },
                        },
                        "joinConfiguration": {
                            "nodeRegistration": {
                                "kubeletExtraArgs": _kubelet_extra_args(),
                            },
                        },
                    },
                },
            },
        },
    )

    control_plane_machine_template = _docker_machine_template(
        control_plane_template_name,
        "cluster-control-plane-machine-template",
    )
    head_machine_template = _docker_machine_template(
        head_machine_template_name,
        "cluster-head-machine-template",
    )
    compute_machine_template = _docker_machine_template(
        compute_machine_template_name,
        "cluster-compute-machine-template",
    )
    head_bootstrap_template = _kubeadm_config_template(
        head_bootstrap_template_name,
        "cluster-head-bootstrap-template",
        controller=True,
    )
    compute_bootstrap_template = _kubeadm_config_template(
        compute_bootstrap_template_name,
        "cluster-compute-bootstrap-template",
    )

    cluster_class = k8s.apiextensions.CustomResource(
        "cluster-class",
        api_version=_CAPI_API_VERSION,
        kind="ClusterClass",
        metadata={"name": cluster_class_name, "namespace": _NAMESPACE},
        spec={
            "infrastructure": {
                "templateRef": _template_ref(
                    _INFRASTRUCTURE_API_VERSION,
                    "DockerClusterTemplate",
                    docker_cluster_template_name,
                ),
            },
            "controlPlane": {
                "healthCheck": _health_check(),
                "machineInfrastructure": {
                    "templateRef": _template_ref(
                        _INFRASTRUCTURE_API_VERSION,
                        "DockerMachineTemplate",
                        control_plane_template_name,
                    ),
                },
                "templateRef": _template_ref(
                    _CONTROL_PLANE_API_VERSION,
                    "KubeadmControlPlaneTemplate",
                    control_plane_template_name,
                ),
            },
            "workers": {
                "machineDeployments": [
                    {
                        "class": "head-node",
                        "bootstrap": {
                            "templateRef": _template_ref(
                                _BOOTSTRAP_API_VERSION,
                                "KubeadmConfigTemplate",
                                head_bootstrap_template_name,
                            ),
                        },
                        "infrastructure": {
                            "templateRef": _template_ref(
                                _INFRASTRUCTURE_API_VERSION,
                                "DockerMachineTemplate",
                                head_machine_template_name,
                            ),
                        },
                        "healthCheck": _health_check(),
                    },
                    {
                        "class": "compute-node",
                        "bootstrap": {
                            "templateRef": _template_ref(
                                _BOOTSTRAP_API_VERSION,
                                "KubeadmConfigTemplate",
                                compute_bootstrap_template_name,
                            ),
                        },
                        "infrastructure": {
                            "templateRef": _template_ref(
                                _INFRASTRUCTURE_API_VERSION,
                                "DockerMachineTemplate",
                                compute_machine_template_name,
                            ),
                        },
                        "healthCheck": _health_check(),
                    },
                ],
            },
            "patches": [
                {
                    "name": "customImage",
                    "description": "Sets the kind node image used by control-plane and MachineDeployment nodes.",
                    "definitions": [
                        {
                            "selector": {
                                "apiVersion": _INFRASTRUCTURE_API_VERSION,
                                "kind": "DockerMachineTemplate",
                                "matchResources": {"controlPlane": True},
                            },
                            "jsonPatches": [
                                {
                                    "op": "add",
                                    "path": "/spec/template/spec/customImage",
                                    "valueFrom": {
                                        "template": 'kindest/node:{{ .builtin.controlPlane.version | replace "+" "_" }}',
                                    },
                                }
                            ],
                        },
                        {
                            "selector": {
                                "apiVersion": _INFRASTRUCTURE_API_VERSION,
                                "kind": "DockerMachineTemplate",
                                "matchResources": {
                                    "machineDeploymentClass": {
                                        "names": ["head-node", "compute-node"],
                                    },
                                },
                            },
                            "jsonPatches": [
                                {
                                    "op": "add",
                                    "path": "/spec/template/spec/customImage",
                                    "valueFrom": {
                                        "template": 'kindest/node:{{ .builtin.machineDeployment.version | replace "+" "_" }}',
                                    },
                                }
                            ],
                        },
                    ],
                }
            ],
        },
        opts=pulumi.ResourceOptions(
            depends_on=[
                docker_cluster_template,
                control_plane_template,
                control_plane_machine_template,
                head_machine_template,
                compute_machine_template,
                head_bootstrap_template,
                compute_bootstrap_template,
            ]
        ),
    )

    cluster = k8s.apiextensions.CustomResource(
        "cluster",
        api_version=_CAPI_API_VERSION,
        kind="Cluster",
        metadata={
            "name": cluster_name,
            "namespace": _NAMESPACE,
            "annotations": {_SKIP_AWAIT_ANNOTATION: "true"},
        },
        spec={
            "clusterNetwork": {
                "pods": {"cidrBlocks": [_POD_CIDR]},
                "services": {"cidrBlocks": [_SERVICE_CIDR]},
                "serviceDomain": _SERVICE_DOMAIN,
            },
            "topology": {
                "classRef": {"name": cluster_class_name},
                "version": _KUBERNETES_VERSION,
                "controlPlane": {"replicas": 1},
                "workers": {
                    "machineDeployments": [
                        {
                            "class": "head-node",
                            "name": "head",
                            "replicas": 1,
                            "metadata": {
                                "labels": {
                                    _NODE_TYPE_LABEL: _CONTROLLER_NODE_TYPE,
                                },
                            },
                        },
                        {
                            "class": "compute-node",
                            "name": "compute",
                            "metadata": {
                                "annotations": {
                                    _AUTOSCALER_MIN_ANNOTATION: "1",
                                    _AUTOSCALER_MAX_ANNOTATION: "10",
                                },
                                "labels": {_NODE_TYPE_LABEL: _COMPUTE_NODE_TYPE},
                            },
                        },
                    ],
                },
            },
        },
        opts=pulumi.ResourceOptions(depends_on=[cluster_class]),
    )

    workload_kubeconfig = KubeconfigSecret(
        "workload-kubeconfig",
        namespace=_NAMESPACE,
        secret_name=f"{cluster_name}-kubeconfig",
        opts=pulumi.ResourceOptions(depends_on=[cluster]),
    )
    workload_provider = k8s.Provider(
        "workload-k8s",
        kubeconfig=workload_kubeconfig.kubeconfig,
        opts=pulumi.ResourceOptions(depends_on=[workload_kubeconfig]),
    )

    calico_namespace = k8s.core.v1.Namespace(
        "calico-operator-namespace",
        metadata={
            "name": _CALICO_OPERATOR_NAMESPACE,
            "labels": {"pod-security.kubernetes.io/enforce": "privileged"},
        },
        opts=pulumi.ResourceOptions(provider=workload_provider),
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
        ),
    )

    # Echo the tenant + a marker so ``pulumi stack output`` confirms
    # the dispatcher routed correctly.
    pulumi.export("tenant", _TENANT)
    pulumi.export("cluster_name", cluster.metadata["name"])
    pulumi.export("cluster_class_name", cluster_class.metadata["name"])
    pulumi.export("calico_operator_chart_version", _CALICO_CHART_VERSION)
    pulumi.export("calico_operator_status", calico_operator.status)
    pulumi.export("workload_cluster_ready", False)
    pulumi.export(
        "todo",
        "Install slurm-operator and NodeSets on the Calico-enabled workload cluster.",
    )
