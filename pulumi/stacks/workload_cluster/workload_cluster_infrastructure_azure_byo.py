"""Azure BYO workload infrastructure owned by the PKO init stack."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping

import pulumi
import pulumi_azure_native as azure_native
import pulumi_kubernetes as k8s
from pydantic import BaseModel, ConfigDict, StrictBool

from lib.config import NonEmptyStr, PulumiConfigModel, StrictPositiveInt
from localenv import AzureHostNetwork
from stacks.kubernetes_annotations import (
    PULUMI_SKIP_AWAIT_ANNOTATION,
    foreground_delete_annotations,
    pulumi_wait_for,
)
from stacks.workload_cluster.workload_cluster_addons import (
    AzureCloudProvider,
    CalicoVXLAN,
)
from stacks.workload_cluster.workload_cluster_infrastructure import (
    AUTOSCALER_MAX_ANNOTATION,
    AUTOSCALER_MIN_ANNOTATION,
    ClusterAPIAutoscaler,
    ClusterAPIAutoscalerOutputs,
    machine_deployment_labels,
    worker_labels,
)
from stacks.workload_cluster.workload_cluster_storage import LocalPathStorage


_FLEX_ORCHESTRATION_MODE = "Flexible"
# Availability zone for the Flex VMSS, the internal API-server LB, and the
# machine failureDomain. West Europe offers the RDMA GPU SKUs (e.g.
# Standard_ND96amsr_A100_v4) in zone 2, and the D-series control-plane / CPU
# SKUs are available there too. TODO(caps): make this a per-run config
# value once multi-region/zone provisioning is needed (SKU zones vary by region).
_DEFAULT_FLEX_ZONE = "2"
_DEFAULT_PLATFORM_FAULT_DOMAIN_COUNT = 1
_CAPI_API_VERSION = "cluster.x-k8s.io/v1beta1"
_BOOTSTRAP_API_VERSION = "bootstrap.cluster.x-k8s.io/v1beta1"
_CONTROL_PLANE_API_VERSION = "controlplane.cluster.x-k8s.io/v1beta1"
_CONTROL_PLANE_READY_API_VERSION = "controlplane.cluster.x-k8s.io/v1beta2"
_INFRASTRUCTURE_API_VERSION = "infrastructure.cluster.x-k8s.io/v1beta1"
_NAMESPACE = "default"
_POD_CIDR = "192.168.0.0/16"
_SERVICE_CIDR = "10.96.0.0/12"
_SERVICE_DOMAIN = "cluster.local"
_WORKLOAD_KUBECONFIG_SECRET_KEY = "value"
_WAIT_FOR_CONTROL_PLANE_INITIALIZED = "condition=Initialized"
_WAIT_FOR_CONTROL_PLANE_AVAILABLE = "condition=ControlPlaneReady"
_CAPI_LIFECYCLE_TIMEOUT = "60m"
_AZURE_PROVIDER_ID_PREFIX = "azure://"
_DNS_LABEL_MAX_LENGTH = 63
_DNS_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9]+")

# Azure Security Pack (AzSecPack) auto-config onboarding tag. Corp security policy
# requires it on publicly-exposed Azure resources (the self-managed manifests set
# it on every VMSS/VM). Applied whenever the API server is public so the CAPZ-
# managed public IP and the control-plane/worker VMs carry the required security
# tag (MDM/MDSD onboarding). TODO(caps): if org policy also mandates it on internal
# VMs, make it unconditional (the manifests set it always).
_AZSECPACK_TAG_KEY = "AzSecPackAutoConfigReady"
_AZSECPACK_TAG_VALUE = "true"


def _effective_tags(
    additional_tags: Mapping[str, str],
    *,
    api_server_public: bool,
) -> dict[str, str]:
    """Fold the AzSecPack tag into the caller tags when the API server is public."""
    tags = dict(additional_tags)
    if api_server_public:
        tags[_AZSECPACK_TAG_KEY] = _AZSECPACK_TAG_VALUE
    return tags


def _cluster_annotations() -> dict[str, str]:
    return foreground_delete_annotations(
        {PULUMI_SKIP_AWAIT_ANNOTATION: "true"}
    )


def _control_plane_annotations() -> dict[str, str]:
    return foreground_delete_annotations(
        {PULUMI_SKIP_AWAIT_ANNOTATION: "true"}
    )


def _control_plane_ready_annotations() -> dict[str, str]:
    return pulumi_wait_for(_WAIT_FOR_CONTROL_PLANE_INITIALIZED)


class AzureBYOMarketplaceImage(PulumiConfigModel):
    """Azure Marketplace image reference for an Azure BYO node pool.

    When unset, CAPZ boots its default reference Ubuntu image, which has no GPU
    driver / HPC-X / NCCL / InfiniBand tooling and therefore cannot run the GPU
    benchmark path. NDV2/V100 requires a V100-compatible image such as the
    ``microsoft-dsvm`` ``ubuntu-hpc`` ``2404-v100`` SKU (ships HPC-X, OFED,
    CUDA/NCCL, nccl-tests, /dev/infiniband).

    TODO(caps): do NOT assume a newer GPU image boots on NDV2 -- keep sku/version
    explicit per target SKU. Newer GPU SKUs (A100/H100) need their own images.
    """

    publisher: NonEmptyStr
    offer: NonEmptyStr
    sku: NonEmptyStr
    version: NonEmptyStr

    def to_marketplace(self) -> dict[str, str]:
        """Render the CAPZ ``image.marketplace`` block."""
        return {
            "publisher": self.publisher,
            "offer": self.offer,
            "sku": self.sku,
            "version": self.version,
        }


class AzureBYONodePoolSpec(PulumiConfigModel):
    """One self-managed Azure node role rendered through CAPI resources."""

    name: NonEmptyStr
    node_type: NonEmptyStr
    vm_size: NonEmptyStr
    replicas: StrictPositiveInt
    controller: StrictBool = False
    attach_to_flex: StrictBool = False
    failure_domain: NonEmptyStr = _DEFAULT_FLEX_ZONE
    autoscaler_bounds: tuple[StrictPositiveInt, StrictPositiveInt] | None = None
    # Optional V100-compatible marketplace image for the NDV2 GPU/NCCL path; None keeps
    # the CAPZ default Ubuntu image (CPU-only clusters, e.g. caps-val).
    image: AzureBYOMarketplaceImage | None = None
    # Optional Shared Image Gallery image-version resource ID to boot instead (e.g. the
    # HPC image under validation). Rendered as CAPZ image.id and takes precedence over
    # ``image``. The image carries no Kubernetes; the bootstrap config installs it.
    image_id: NonEmptyStr | None = None


def _autoscaler_annotations(
    bounds: tuple[int, int] | None,
) -> dict[str, str]:
    if bounds is None:
        return {}
    min_replicas, max_replicas = bounds
    if min_replicas > max_replicas:
        raise ValueError("autoscaler minimum replicas must not exceed maximum")
    return {
        AUTOSCALER_MIN_ANNOTATION: str(min_replicas),
        AUTOSCALER_MAX_ANNOTATION: str(max_replicas),
    }


class AzureBYOSubnet(BaseModel):
    """Resolved existing subnet consumed by the Azure BYO CAPI graph."""

    model_config = ConfigDict(frozen=True)

    subscription_id: str
    location: str
    vnet_id: str
    vnet_name: str
    vnet_resource_group: str
    subnet_id: str
    subnet_name: str
    address_prefix: str | None = None
    network_security_group_id: str | None = None
    nat_gateway_id: str | None = None
    route_table_id: str | None = None

    @classmethod
    def from_host_network(cls, network: AzureHostNetwork) -> AzureBYOSubnet:
        return cls(
            subscription_id=network.subscription_id,
            location=network.location,
            vnet_id=network.vnet_id,
            vnet_name=network.vnet_name,
            vnet_resource_group=network.vnet_resource_group,
            subnet_id=network.subnet_id,
            subnet_name=network.subnet_name,
            address_prefix=network.subnet_address_prefix,
            network_security_group_id=network.network_security_group_id,
            nat_gateway_id=network.nat_gateway_id,
            route_table_id=network.route_table_id,
        )


def _resource_name(instance: str, suffix: str) -> str:
    normalized = _DNS_LABEL_INVALID_CHARS.sub("-", instance.lower()).strip("-")
    if not normalized:
        raise ValueError("instance must contain at least one alphanumeric character")
    normalized_suffix = _DNS_LABEL_INVALID_CHARS.sub(
        "-", suffix.lower()
    ).strip("-")
    if not normalized_suffix:
        raise ValueError("resource suffix must contain at least one alphanumeric character")
    if len(normalized_suffix) >= _DNS_LABEL_MAX_LENGTH:
        raise ValueError("resource suffix must be shorter than 63 characters")
    normalized = normalized[
        : _DNS_LABEL_MAX_LENGTH - len(normalized_suffix) - 1
    ].rstrip("-")
    return f"{normalized}-{normalized_suffix}"


def _cluster_name(instance: str) -> str:
    normalized = _DNS_LABEL_INVALID_CHARS.sub("-", instance.lower()).strip("-")
    if not normalized:
        raise ValueError("instance must contain at least one alphanumeric character")
    return normalized[:_DNS_LABEL_MAX_LENGTH].rstrip("-")


def _partition_node_pools(
    node_pools: tuple[AzureBYONodePoolSpec, ...],
) -> tuple[AzureBYONodePoolSpec, tuple[AzureBYONodePoolSpec, ...]]:
    controllers = tuple(node for node in node_pools if node.controller)
    workers = tuple(node for node in node_pools if not node.controller)
    if len(controllers) != 1:
        raise ValueError("azure-byo requires exactly one controller node spec")
    if not workers:
        raise ValueError("azure-byo requires at least one worker node spec")
    return controllers[0], workers


def _validate_node_pool_names(
    instance: str,
    node_pools: tuple[AzureBYONodePoolSpec, ...],
) -> None:
    logical_names = [node.name for node in node_pools]
    if len(logical_names) != len(set(logical_names)):
        raise ValueError("azure-byo node pool names must be unique")
    resource_names = [_resource_name(instance, node.name) for node in node_pools]
    if len(resource_names) != len(set(resource_names)):
        raise ValueError(
            "azure-byo node pool names must remain unique after normalization"
        )


def _resource_group_name(instance: str) -> str:
    return _resource_name(instance, "rg")


def _vmss_flex_name(instance: str) -> str:
    return _resource_name(instance, "flex")


def _object_ref(
    api_version: str,
    kind: str,
    name: pulumi.Input[str],
) -> dict[str, object]:
    return {"apiVersion": api_version, "kind": kind, "name": name}


def _resource_id_name(resource_id: str | None) -> str | None:
    return resource_id.rstrip("/").rsplit("/", 1)[-1] if resource_id else None


def _cluster_spec(*, cluster_name: str, control_plane_name: str) -> dict[str, object]:
    return {
        "clusterNetwork": {
            "pods": {"cidrBlocks": [_POD_CIDR]},
            "services": {"cidrBlocks": [_SERVICE_CIDR]},
            "serviceDomain": _SERVICE_DOMAIN,
        },
        "controlPlaneRef": _object_ref(
            _CONTROL_PLANE_API_VERSION,
            "KubeadmControlPlane",
            control_plane_name,
        ),
        "infrastructureRef": _object_ref(
            _INFRASTRUCTURE_API_VERSION,
            "AzureCluster",
            cluster_name,
        ),
    }


def _azure_cluster_spec(
    *,
    cluster_name: str,
    identity_name: pulumi.Input[str],
    identity_namespace: pulumi.Input[str],
    location: str,
    resource_group: pulumi.Input[str],
    subscription_id: str,
    subnet: AzureBYOSubnet,
    additional_tags: Mapping[str, str],
    api_server_public: bool = False,
) -> dict[str, object]:
    cluster_subnet: dict[str, object] = {
        "name": subnet.subnet_name,
        "role": "cluster",
    }
    security_group_name = _resource_id_name(subnet.network_security_group_id)
    route_table_name = _resource_id_name(subnet.route_table_id)
    nat_gateway_name = _resource_id_name(subnet.nat_gateway_id)
    if security_group_name is not None:
        cluster_subnet["securityGroup"] = {"name": security_group_name}
    if route_table_name is not None:
        cluster_subnet["routeTable"] = {"name": route_table_name}
    if nat_gateway_name is not None:
        cluster_subnet["natGateway"] = {"name": nat_gateway_name}

    # Head-node (control-plane) API server exposure. Default = Internal LB reached
    # via the mgmt VM (the caps-self model). When public, CAPZ fronts the API
    # server with a public LB + public IP, and CAPZ propagates additionalTags
    # (including the AzSecPack tag) onto that public IP.
    if api_server_public:
        api_server_lb: dict[str, object] = {
            "name": f"{cluster_name}-public-lb",
            "type": "Public",
        }
    else:
        api_server_lb = {
            "name": f"{cluster_name}-internal-lb",
            "type": "Internal",
            "availabilityZones": [_DEFAULT_FLEX_ZONE],
        }

    return {
        "identityRef": {
            **_object_ref(
                _INFRASTRUCTURE_API_VERSION,
                "AzureClusterIdentity",
                identity_name,
            ),
            "namespace": identity_namespace,
        },
        "location": location,
        "resourceGroup": resource_group,
        "subscriptionID": subscription_id,
        "networkSpec": {
            "vnet": {
                "name": subnet.vnet_name,
                "resourceGroup": subnet.vnet_resource_group,
            },
            "subnets": [cluster_subnet],
            "apiServerLB": api_server_lb,
            "controlPlaneOutboundLB": {"frontendIPsCount": 1},
            "nodeOutboundLB": {"frontendIPsCount": 1},
        },
        "additionalTags": dict(additional_tags),
    }


def _machine_template_spec(
    *,
    node: AzureBYONodePoolSpec,
    subnet_name: str,
    node_identity_provider_id: pulumi.Input[str],
    additional_tags: Mapping[str, str],
    virtual_machine_scale_set_id: pulumi.Input[str] | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "vmSize": node.vm_size,
        "osDisk": {
            "diskSizeGB": 128,
            "managedDisk": {"storageAccountType": "Premium_LRS"},
            "osType": "Linux",
        },
        "identity": "UserAssigned",
        "userAssignedIdentities": [{"providerID": node_identity_provider_id}],
        "networkInterfaces": [{"subnetName": subnet_name}],
        "additionalTags": dict(additional_tags),
    }
    if node.image_id is not None:
        # Boot a Shared Image Gallery image-version by resource ID (e.g. the HPC image
        # under validation). It ships no Kubernetes; the bootstrap config installs it.
        spec["image"] = {"id": node.image_id}
    elif node.image is not None:
        # NDV2/V100 nodes must boot a V100-compatible image (HPC-X/CUDA/NCCL);
        # the default CAPZ Ubuntu image has no GPU/IB tooling.
        spec["image"] = {"marketplace": node.image.to_marketplace()}
    if virtual_machine_scale_set_id is not None:
        spec["virtualMachineScaleSetID"] = virtual_machine_scale_set_id
    return {"template": {"spec": spec}}


def _cloud_config_file(*, secret_name: str, key: str) -> dict[str, object]:
    return {
        "contentFrom": {"secret": {"name": secret_name, "key": key}},
        "owner": "root:root",
        "path": "/etc/kubernetes/azure.json",
        "permissions": "0644",
    }


def _node_registration(*, node_type: str | None = None) -> dict[str, object]:
    extra_args: dict[str, str] = {"cloud-provider": "external"}
    if node_type is not None:
        extra_args["node-labels"] = f"slinky.slurm.net/node-type={node_type}"
    return {
        "name": '{{ ds.meta_data["local_hostname"] }}',
        "kubeletExtraArgs": extra_args,
    }


def _ssh_users(
    *,
    ssh_username: str,
    ssh_authorized_keys: tuple[str, ...],
) -> list[dict[str, object]]:
    if not ssh_authorized_keys:
        return []
    return [
        {
            "name": ssh_username,
            "sshAuthorizedKeys": list(ssh_authorized_keys),
        }
    ]


def _node_needs_kubernetes_install(node: AzureBYONodePoolSpec) -> bool:
    """True when the node boots an explicit image that ships no Kubernetes.

    A Shared Image Gallery image (``image_id``, e.g. the azhpc HPC image under
    validation) or a Marketplace image carries GPU/IB bits but no kubeadm. The
    default CAPI reference image has kubeadm/kubelet/containerd baked in, so those
    nodes skip the install.
    """
    return node.image_id is not None or node.image is not None


def _kubernetes_install_commands(
    kubernetes_version: str,
    *,
    ssh_username: str = "capi",
    configure_nvidia_runtime: bool = False,
) -> list[str]:
    """preKubeadmCommands that install containerd + pinned kubelet/kubeadm/kubectl.

    For a node booting an image with no Kubernetes (the HPC image under validation),
    CAPZ installs Kubernetes at bootstrap so ``kubeadm init``/``join`` can run. The apt
    (Debian/Ubuntu) path mirrors the proven list in selfmanaged-validation.flex.yaml; the
    dnf (AlmaLinux/RHEL) and tdnf (AzureLinux) paths follow upstream's Kubernetes RPM
    instructions. cloud-init concatenates preKubeadmCommands into a single script, so the
    leading ``set -eux`` and the ``case "$ID"`` OS switch persist across the whole list.

    NOTE: only the apt path is validated end-to-end (the azhpc images under test are
    Ubuntu-based today); the RPM paths are provided per upstream docs and untested here.
    """
    version = kubernetes_version.lstrip("v")  # e.g. 1.36.1
    minor = "v" + ".".join(version.split(".")[:2])  # e.g. v1.36
    deb_pkg = f"{version}-1.1"  # pkgs.k8s.io deb revision, e.g. 1.36.1-1.1
    deb_repo = f"https://pkgs.k8s.io/core:/stable:/{minor}/deb/"
    rpm_repo = f"https://pkgs.k8s.io/core:/stable:/{minor}/rpm/"
    rpm_repo_file = (
        "printf '[kubernetes]\\nname=Kubernetes\\n"
        f"baseurl={rpm_repo}\\nenabled=1\\ngpgcheck=1\\n"
        f"gpgkey={rpm_repo}repodata/repomd.xml.key\\n' > /etc/yum.repos.d/kubernetes.repo"
    )
    # OS-aware containerd + kubelet/kubeadm/kubectl install (one shell `case`).
    install_block = "\n".join(
        [
            ". /etc/os-release",
            'case "${ID:-}" in',
            "  ubuntu|debian)",
            "    export DEBIAN_FRONTEND=noninteractive",
            "    apt-get -o DPkg::Lock::Timeout=600 update -y",
            "    apt-get -o DPkg::Lock::Timeout=600 install -y apt-transport-https ca-certificates curl gpg",
            "    command -v containerd >/dev/null 2>&1 || apt-get -o DPkg::Lock::Timeout=600 install -y containerd",
            "    mkdir -p /etc/apt/keyrings",
            f"    curl -fsSL {deb_repo}Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg",
            f"    echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] {deb_repo} /' > /etc/apt/sources.list.d/kubernetes.list",
            "    apt-get -o DPkg::Lock::Timeout=600 update -y",
            f"    apt-get -o DPkg::Lock::Timeout=600 install -y kubelet={deb_pkg} kubeadm={deb_pkg} kubectl={deb_pkg}",
            "    apt-mark hold kubelet kubeadm kubectl",
            "    ;;",
            "  almalinux|rhel|centos|rocky|fedora)",
            f"    {rpm_repo_file}",
            "    command -v containerd >/dev/null 2>&1 || dnf install -y containerd || dnf install -y containerd.io",
            f"    dnf install -y --disableexcludes=kubernetes kubelet-{version} kubeadm-{version} kubectl-{version}",
            "    (dnf install -y python3-dnf-plugin-versionlock && dnf versionlock add kubelet kubeadm kubectl) || true",
            "    ;;",
            "  azurelinux|mariner)",
            f"    {rpm_repo_file}",
            "    command -v containerd >/dev/null 2>&1 || tdnf install -y containerd",
            f"    tdnf install -y kubelet-{version} kubeadm-{version} kubectl-{version}",
            "    ;;",
            "  *)",
            '    echo "caps: unsupported OS for kubernetes install: ${ID:-unknown}" >&2; exit 1',
            "    ;;",
            "esac",
        ]
    )
    commands = [
        "set -eux",
        "modprobe overlay",
        "modprobe br_netfilter",
        "printf 'overlay\\nbr_netfilter\\n' > /etc/modules-load.d/k8s.conf",
        (
            "printf 'net.bridge.bridge-nf-call-iptables=1\\n"
            "net.bridge.bridge-nf-call-ip6tables=1\\n"
            "net.ipv4.ip_forward=1\\n' > /etc/sysctl.d/k8s.conf"
        ),
        "sysctl --system",
        "swapoff -a",
        r"sed -ri '/\sswap\s/s/^/#/' /etc/fstab",
        install_block,
        "mkdir -p /etc/containerd",
        "containerd config default > /etc/containerd/config.toml",
        "sed -ri 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml",
    ]
    if configure_nvidia_runtime:
        commands.append(
            "if command -v nvidia-ctk >/dev/null 2>&1; then "
            "nvidia-ctk runtime configure --runtime=containerd; fi"
        )
    commands.extend(
        [
            "systemctl restart containerd",
            "systemctl enable containerd",
            "systemctl enable kubelet",
            (
                f"if getent group docker >/dev/null 2>&1 && id -u {ssh_username!r} "
                f">/dev/null 2>&1; then usermod -aG docker {ssh_username!r}; fi"
            ),
        ]
    )
    return commands


def _kubeadm_control_plane_spec(
    *,
    node: AzureBYONodePoolSpec,
    cluster_name: str,
    control_plane_name: str,
    kubernetes_version: str,
    ssh_username: str = "capi",
    ssh_authorized_keys: tuple[str, ...] = (),
) -> dict[str, object]:
    # Restore the apiserver hostname the kubeadm files reference. When the control plane
    # boots an image without Kubernetes (SIG/Marketplace), install containerd + kubelet/
    # kubeadm/kubectl first so `kubeadm init` can run.
    control_plane_pre_kubeadm: list[str] = [
        (
            "if [ -f /tmp/kubeadm.yaml ] || "
            "[ -f /run/kubeadm/kubeadm.yaml ]; then "
            f"echo '127.0.0.1 apiserver.{cluster_name}.capz.io apiserver' "
            ">> /etc/hosts; fi"
        )
    ]
    if _node_needs_kubernetes_install(node):
        control_plane_pre_kubeadm = (
            _kubernetes_install_commands(
                kubernetes_version,
                ssh_username=ssh_username,
            )
            + control_plane_pre_kubeadm
        )
    kubeadm_config_spec: dict[str, object] = {
        "clusterConfiguration": {
            "controllerManager": {
                "extraArgs": {
                    "allocate-node-cidrs": "false",
                    "cloud-provider": "external",
                    "cluster-name": cluster_name,
                }
            }
        },
        "files": [
            _cloud_config_file(
                secret_name=f"{control_plane_name}-azure-json",
                key="control-plane-azure.json",
            )
        ],
        "initConfiguration": {
            "nodeRegistration": _node_registration(node_type=node.node_type)
        },
        "joinConfiguration": {
            "nodeRegistration": _node_registration(node_type=node.node_type)
        },
        "preKubeadmCommands": control_plane_pre_kubeadm,
        "postKubeadmCommands": [
            (
                "if [ -f /tmp/kubeadm-join-config.yaml ] || "
                "[ -f /run/kubeadm/kubeadm-join-config.yaml ]; then "
                f"echo '127.0.0.1 apiserver.{cluster_name}.capz.io apiserver' "
                ">> /etc/hosts; fi"
            )
        ],
    }
    ssh_users = _ssh_users(
        ssh_username=ssh_username,
        ssh_authorized_keys=ssh_authorized_keys,
    )
    if ssh_users:
        kubeadm_config_spec["users"] = ssh_users

    return {
        "replicas": node.replicas,
        "version": kubernetes_version,
        "machineTemplate": {
            "infrastructureRef": _object_ref(
                _INFRASTRUCTURE_API_VERSION,
                "AzureMachineTemplate",
                control_plane_name,
            ),
        },
        "kubeadmConfigSpec": kubeadm_config_spec,
    }


def _machine_deployment_spec(
    *,
    node: AzureBYONodePoolSpec,
    cluster_name: str,
    worker_name: str,
    kubernetes_version: str,
) -> dict[str, object]:
    labels = {
        **worker_labels(cluster_name, node.node_type),
        f"{cluster_name}.worker": node.name,
    }
    autoscaler_annotations = _autoscaler_annotations(node.autoscaler_bounds)
    return {
        "clusterName": cluster_name,
        "replicas": node.replicas,
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
                "version": kubernetes_version,
                "failureDomain": node.failure_domain,
                "bootstrap": {
                    "configRef": _object_ref(
                        _BOOTSTRAP_API_VERSION,
                        "KubeadmConfigTemplate",
                        worker_name,
                    )
                },
                "infrastructureRef": _object_ref(
                    _INFRASTRUCTURE_API_VERSION,
                    "AzureMachineTemplate",
                    worker_name,
                ),
            },
        },
    }


def _kubeadm_config_template_spec(
    *,
    node: AzureBYONodePoolSpec,
    worker_name: str,
    kubernetes_version: str,
    ssh_username: str = "capi",
    ssh_authorized_keys: tuple[str, ...] = (),
) -> dict[str, object]:
    template_spec: dict[str, object] = {
        "files": [
            _cloud_config_file(
                secret_name=f"{worker_name}-azure-json",
                key="worker-node-azure.json",
            )
        ],
        "joinConfiguration": {
            "nodeRegistration": _node_registration(node_type=node.node_type)
        },
    }
    if _node_needs_kubernetes_install(node):
        # The worker image (SIG/Marketplace, e.g. the HPC image under validation) ships no
        # Kubernetes; install containerd + kubelet/kubeadm/kubectl before `kubeadm join`.
        template_spec["preKubeadmCommands"] = _kubernetes_install_commands(
            kubernetes_version,
            ssh_username=ssh_username,
            configure_nvidia_runtime=True,
        )
    ssh_users = _ssh_users(
        ssh_username=ssh_username,
        ssh_authorized_keys=ssh_authorized_keys,
    )
    if ssh_users:
        template_spec["users"] = ssh_users

    return {
        "template": {
            "spec": template_spec,
        }
    }


def _decode_secret_data_value(data: Mapping[str, str], key: str) -> str:
    encoded_value = data.get(key)
    if not encoded_value:
        raise KeyError(f"Secret data[{key!r}] is missing")
    return base64.b64decode(encoded_value).decode("utf-8")


def _resource_group_args(
    *,
    instance: str,
    location: str,
    additional_tags: Mapping[str, str],
) -> azure_native.resources.ResourceGroupArgs:
    return azure_native.resources.ResourceGroupArgs(
        resource_group_name=_resource_group_name(instance),
        location=location,
        tags=dict(additional_tags),
    )


def _vmss_flex_args(
    *,
    instance: str,
    location: str,
    resource_group: pulumi.Input[str],
    additional_tags: Mapping[str, str],
) -> azure_native.compute.VirtualMachineScaleSetArgs:
    """Azure Native inputs for an empty Flex placement container.

    The VMSS intentionally has no virtual-machine profile. CAPZ creates each
    AzureMachine independently and attaches it through the fork's
    ``virtualMachineScaleSetID`` field.
    """
    return azure_native.compute.VirtualMachineScaleSetArgs(
        resource_group_name=resource_group,
        vm_scale_set_name=_vmss_flex_name(instance),
        location=location,
        orchestration_mode=_FLEX_ORCHESTRATION_MODE,
        platform_fault_domain_count=_DEFAULT_PLATFORM_FAULT_DOMAIN_COUNT,
        zones=[_DEFAULT_FLEX_ZONE],
        tags=dict(additional_tags),
    )


class AzureBYOWorkloadClusterInfrastructure(pulumi.ComponentResource):
    """Create Azure resources that CAPZ consumes as BYO worker infrastructure."""

    resource_group_id: pulumi.Output[str]
    resource_group_name: pulumi.Output[str]
    vmss_flex_id: pulumi.Output[str]
    vmss_flex_name: pulumi.Output[str]
    byo_subnet: AzureBYOSubnet | None
    cluster_name: pulumi.Output[str]
    control_plane_name: pulumi.Output[str]
    worker_machine_deployment_name: pulumi.Output[str]
    worker_machine_deployment_names: list[pulumi.Output[str]]
    control_plane_ready: pulumi.Output[bool]
    workload_kubeconfig: pulumi.Output[str]
    workload_provider: k8s.Provider
    azure_cloud_provider_chart_version: pulumi.Output[str]
    azure_cloud_provider_status: pulumi.Output[object]
    calico_chart_version: pulumi.Output[str]
    calico_status: pulumi.Output[object]
    local_path_storage_class_name: pulumi.Output[str]
    cluster_autoscaler: pulumi.Output[ClusterAPIAutoscalerOutputs] | None
    workload_cluster_ready: pulumi.Output[bool]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        subscription_id: str,
        tenant_id: pulumi.Input[str],
        client_id: pulumi.Input[str],
        node_identity_resource_id: pulumi.Input[str],
        identity_name: pulumi.Input[str],
        identity_namespace: pulumi.Input[str],
        location: str,
        additional_tags: Mapping[str, str],
        api_server_public: bool = False,
        resource_group_name: str | None = None,
        byo_subnet: AzureBYOSubnet | None = None,
        kubernetes_version: str,
        ssh_username: str = "capi",
        ssh_authorized_keys: tuple[str, ...] = (),
        node_pools: tuple[AzureBYONodePoolSpec, ...],
        provider: k8s.Provider | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:AzureBYOWorkloadClusterInfrastructure",
            name,
            props={},
            opts=opts,
        )

        controller_node, worker_nodes = _partition_node_pools(node_pools)
        _validate_node_pool_names(instance, node_pools)
        if byo_subnet is None:
            raise ValueError(
                "azure-byo requires an explicit or auto-discovered VNet/subnet"
            )

        # A public API server exposes the head node; corp security policy then
        # requires the AzSecPack onboarding tag on the exposed Azure resources.
        additional_tags = _effective_tags(
            additional_tags, api_server_public=api_server_public
        )

        azure_provider = azure_native.Provider(
            "azure-native",
            subscription_id=subscription_id,
            tenant_id=tenant_id,
            client_id=client_id,
            use_msi=True,
            opts=pulumi.ResourceOptions(parent=self),
        )
        if resource_group_name is None:
            resource_group_resource = azure_native.resources.ResourceGroup(
                "resource-group",
                _resource_group_args(
                    instance=instance,
                    location=location,
                    additional_tags=additional_tags,
                ),
                opts=pulumi.ResourceOptions(
                    parent=self,
                    provider=azure_provider,
                ),
            )
            resource_group_name_output = resource_group_resource.name
            resource_group_id = resource_group_resource.id
            resource_group_dependencies: list[pulumi.Input[pulumi.Resource]] = [
                resource_group_resource
            ]
        else:
            # Reuse a resource group that exists OUTSIDE this Pulumi program (the
            # Azure IMDS-discovered host RG). Reference it read-only via an invoke
            # so Pulumi neither creates nor deletes it; child resources still land
            # in it via its name. CAPZ leaves the untagged shared RG unmanaged.
            existing_resource_group = azure_native.resources.get_resource_group_output(
                resource_group_name=resource_group_name,
                opts=pulumi.InvokeOutputOptions(
                    parent=self,
                    provider=azure_provider,
                ),
            )
            resource_group_name_output = existing_resource_group.name
            resource_group_id = existing_resource_group.id
            resource_group_dependencies = []
        flex = azure_native.compute.VirtualMachineScaleSet(
            "vmss-flex",
            _vmss_flex_args(
                instance=instance,
                location=location,
                resource_group=resource_group_name_output,
                additional_tags=additional_tags,
            ),
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=azure_provider,
                delete_before_replace=True,
                replace_on_changes=[
                    "orchestrationMode",
                    "platformFaultDomainCount",
                    "zones",
                ],
                custom_timeouts=pulumi.CustomTimeouts(
                    create="30m",
                    update="30m",
                    delete="30m",
                ),
            ),
        )

        cluster_name = _cluster_name(instance)
        control_plane_name = _resource_name(instance, controller_node.name)
        node_identity_provider_id = pulumi.Output.concat(
            _AZURE_PROVIDER_ID_PREFIX,
            node_identity_resource_id,
        )

        def child_options(
            *,
            depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
            capi_lifecycle: bool = False,
            ignore_changes: list[str] | None = None,
        ) -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
                ignore_changes=ignore_changes,
                custom_timeouts=(
                    pulumi.CustomTimeouts(
                        create=_CAPI_LIFECYCLE_TIMEOUT,
                        update=_CAPI_LIFECYCLE_TIMEOUT,
                        delete=_CAPI_LIFECYCLE_TIMEOUT,
                    )
                    if capi_lifecycle
                    else None
                ),
            )

        azure_cluster = k8s.apiextensions.CustomResource(
            "azure-cluster",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind="AzureCluster",
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                "annotations": foreground_delete_annotations(),
            },
            spec=_azure_cluster_spec(
                cluster_name=cluster_name,
                identity_name=identity_name,
                identity_namespace=identity_namespace,
                location=location,
                resource_group=resource_group_name_output,
                subscription_id=subscription_id,
                subnet=byo_subnet,
                additional_tags=additional_tags,
                api_server_public=api_server_public,
            ),
            opts=child_options(
                depends_on=resource_group_dependencies,
                capi_lifecycle=True,
            ),
        )
        cluster = k8s.apiextensions.CustomResource(
            "cluster",
            api_version=_CAPI_API_VERSION,
            kind="Cluster",
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                "annotations": _cluster_annotations(),
                "labels": {"cloud-provider": "azure"},
            },
            spec=_cluster_spec(
                cluster_name=cluster_name,
                control_plane_name=control_plane_name,
            ),
            opts=child_options(depends_on=[azure_cluster], capi_lifecycle=True),
        )
        azure_cluster_ready = k8s.apiextensions.CustomResourcePatch(
            "azure-cluster-ready",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind="AzureCluster",
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                "annotations": pulumi_wait_for("condition=Ready"),
            },
            opts=child_options(depends_on=[cluster], capi_lifecycle=True),
        )
        control_plane_machine_template = k8s.apiextensions.CustomResource(
            "control-plane-machine-template",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind="AzureMachineTemplate",
            metadata={"name": control_plane_name, "namespace": _NAMESPACE},
            spec=_machine_template_spec(
                node=controller_node,
                subnet_name=byo_subnet.subnet_name,
                node_identity_provider_id=node_identity_provider_id,
                additional_tags=additional_tags,
            ),
            opts=child_options(
                depends_on=[azure_cluster_ready], capi_lifecycle=True
            ),
        )
        control_plane = k8s.apiextensions.CustomResource(
            "control-plane",
            api_version=_CONTROL_PLANE_API_VERSION,
            kind="KubeadmControlPlane",
            metadata={
                "name": control_plane_name,
                "namespace": _NAMESPACE,
                "annotations": _control_plane_annotations(),
            },
            spec=_kubeadm_control_plane_spec(
                node=controller_node,
                cluster_name=cluster_name,
                control_plane_name=control_plane_name,
                kubernetes_version=kubernetes_version,
                ssh_username=ssh_username,
                ssh_authorized_keys=ssh_authorized_keys,
            ),
            opts=child_options(
                depends_on=[
                    cluster,
                    azure_cluster_ready,
                    control_plane_machine_template,
                ],
                capi_lifecycle=True,
            ),
        )
        control_plane_ready = k8s.apiextensions.CustomResourcePatch(
            "control-plane-ready",
            api_version=_CONTROL_PLANE_READY_API_VERSION,
            kind="KubeadmControlPlane",
            metadata={
                "name": control_plane_name,
                "namespace": _NAMESPACE,
                "annotations": _control_plane_ready_annotations(),
            },
            opts=child_options(
                depends_on=[control_plane],
                capi_lifecycle=True,
            ),
        )
        worker_machine_deployments: list[k8s.apiextensions.CustomResource] = []
        worker_names: list[str] = []
        for worker_node in worker_nodes:
            worker_name = _resource_name(instance, worker_node.name)
            worker_names.append(worker_name)
            autoscaler_annotations = _autoscaler_annotations(
                worker_node.autoscaler_bounds
            )
            template_dependencies: list[pulumi.Input[pulumi.Resource]] = [
                azure_cluster_ready
            ]
            if worker_node.attach_to_flex:
                template_dependencies.append(flex)
            worker_machine_template = k8s.apiextensions.CustomResource(
                f"{worker_node.name}-machine-template",
                api_version=_INFRASTRUCTURE_API_VERSION,
                kind="AzureMachineTemplate",
                metadata={"name": worker_name, "namespace": _NAMESPACE},
                spec=_machine_template_spec(
                    node=worker_node,
                    subnet_name=byo_subnet.subnet_name,
                    node_identity_provider_id=node_identity_provider_id,
                    virtual_machine_scale_set_id=(
                        flex.id if worker_node.attach_to_flex else None
                    ),
                    additional_tags=additional_tags,
                ),
                opts=child_options(
                    depends_on=template_dependencies, capi_lifecycle=True
                ),
            )
            worker_bootstrap_template = k8s.apiextensions.CustomResource(
                f"{worker_node.name}-bootstrap-template",
                api_version=_BOOTSTRAP_API_VERSION,
                kind="KubeadmConfigTemplate",
                metadata={"name": worker_name, "namespace": _NAMESPACE},
                spec=_kubeadm_config_template_spec(
                    node=worker_node,
                    worker_name=worker_name,
                    kubernetes_version=kubernetes_version,
                    ssh_username=ssh_username,
                    ssh_authorized_keys=ssh_authorized_keys,
                ),
                opts=child_options(
                    depends_on=[cluster, azure_cluster_ready],
                    capi_lifecycle=True,
                ),
            )
            worker_machine_deployment = k8s.apiextensions.CustomResource(
                f"{worker_node.name}-machine-deployment",
                api_version=_CAPI_API_VERSION,
                kind="MachineDeployment",
                metadata={
                    "name": worker_name,
                    "namespace": _NAMESPACE,
                    "labels": machine_deployment_labels(
                        cluster_name,
                        worker_node.node_type,
                        autoscaler_enabled=(
                            worker_node.autoscaler_bounds is not None
                        ),
                    ),
                    "annotations": foreground_delete_annotations(
                        autoscaler_annotations
                    ),
                },
                spec=_machine_deployment_spec(
                    node=worker_node,
                    cluster_name=cluster_name,
                    worker_name=worker_name,
                    kubernetes_version=kubernetes_version,
                ),
                opts=child_options(
                    depends_on=[
                        cluster,
                        control_plane,
                        worker_machine_template,
                        worker_bootstrap_template,
                    ],
                    capi_lifecycle=True,
                    ignore_changes=(
                        ["spec.replicas"]
                        if worker_node.autoscaler_bounds is not None
                        else None
                    ),
                ),
            )
            worker_machine_deployments.append(worker_machine_deployment)
        workload_kubeconfig_secret = k8s.core.v1.Secret.get(
            "workload-kubeconfig-secret",
            id=f"{_NAMESPACE}/{cluster_name}-kubeconfig",
            opts=child_options(depends_on=[control_plane_ready]),
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
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[workload_kubeconfig_secret],
            ),
        )
        azure_cloud_provider = AzureCloudProvider(
            "azure-cloud-provider",
            cluster_name=cluster_name,
            pod_cidr=_POD_CIDR,
            provider=workload_provider,
            depends_on=[workload_kubeconfig_secret],
            opts=pulumi.ResourceOptions(parent=self),
        )
        calico = CalicoVXLAN(
            "calico-vxlan",
            pod_cidr=_POD_CIDR,
            provider=workload_provider,
            depends_on=[azure_cloud_provider],
            opts=pulumi.ResourceOptions(parent=self),
        )
        local_path_storage = LocalPathStorage(
            "local-path-storage",
            provider=workload_provider,
            depends_on=[azure_cloud_provider, calico],
            opts=pulumi.ResourceOptions(parent=self),
        )
        cluster_autoscaler_enabled = any(
            worker.autoscaler_bounds is not None for worker in worker_nodes
        )
        cluster_autoscaler: ClusterAPIAutoscaler | None = None
        if cluster_autoscaler_enabled:
            cluster_autoscaler = ClusterAPIAutoscaler(
                "cluster-autoscaler",
                instance=instance,
                cluster_name=cluster_name,
                workload_kubeconfig=workload_kubeconfig,
                provider=provider,
                depends_on=[
                    workload_kubeconfig_secret,
                    *worker_machine_deployments,
                ],
                opts=pulumi.ResourceOptions(parent=self),
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
                depends_on=[azure_cloud_provider, calico, local_path_storage],
                capi_lifecycle=True,
            ),
        )
        worker_available = [
            k8s.apiextensions.CustomResourcePatch(
                f"{worker_node.name}-machine-deployment-available",
                api_version=_CAPI_API_VERSION,
                kind="MachineDeployment",
                metadata={
                    "name": worker_name,
                    "namespace": _NAMESPACE,
                    "annotations": pulumi_wait_for("condition=Available"),
                },
                opts=child_options(
                    depends_on=[
                        worker_machine_deployment,
                        azure_cloud_provider,
                        calico,
                        local_path_storage,
                    ],
                    capi_lifecycle=True,
                ),
            )
            for worker_node, worker_name, worker_machine_deployment in zip(
                worker_nodes,
                worker_names,
                worker_machine_deployments,
                strict=True,
            )
        ]

        self.resource_group_id = resource_group_id
        self.resource_group_name = resource_group_name_output
        self.vmss_flex_id = flex.id
        self.vmss_flex_name = flex.name
        self.byo_subnet = byo_subnet
        self.cluster_name = pulumi.Output.from_input(cluster_name)
        self.control_plane_name = pulumi.Output.from_input(control_plane_name)
        self.worker_machine_deployment_names = [
            pulumi.Output.from_input(worker_name) for worker_name in worker_names
        ]
        self.worker_machine_deployment_name = self.worker_machine_deployment_names[0]
        self.control_plane_ready = cluster_control_plane_available.id.apply(
            lambda _: True
        )
        self.workload_kubeconfig = workload_kubeconfig
        self.workload_provider = workload_provider
        self.azure_cloud_provider_chart_version = azure_cloud_provider.chart_version
        self.azure_cloud_provider_status = azure_cloud_provider.status
        self.calico_chart_version = calico.chart_version
        self.calico_status = calico.status
        self.local_path_storage_class_name = local_path_storage.storage_class_name
        self.cluster_autoscaler = (
            cluster_autoscaler.outputs if cluster_autoscaler is not None else None
        )
        self.workload_cluster_ready = pulumi.Output.all(
            *[worker.id for worker in worker_available]
        ).apply(lambda _: True)
        self.register_outputs(
            {
                "resource_group_id": self.resource_group_id,
                "resource_group_name": self.resource_group_name,
                "vmss_flex_id": self.vmss_flex_id,
                "vmss_flex_name": self.vmss_flex_name,
                "byo_subnet": (
                    self.byo_subnet.model_dump() if self.byo_subnet is not None else None
                ),
                "cluster_name": self.cluster_name,
                "control_plane_name": self.control_plane_name,
                "worker_machine_deployment_name": self.worker_machine_deployment_name,
                "worker_machine_deployment_names": (
                    self.worker_machine_deployment_names
                ),
                "control_plane_ready": self.control_plane_ready,
                "azure_cloud_provider_chart_version": (
                    self.azure_cloud_provider_chart_version
                ),
                "azure_cloud_provider_status": self.azure_cloud_provider_status,
                "calico_chart_version": self.calico_chart_version,
                "calico_status": self.calico_status,
                "local_path_storage_class_name": (
                    self.local_path_storage_class_name
                ),
                "cluster_autoscaler": (
                    self.cluster_autoscaler.apply(
                        lambda outputs: outputs.model_dump()
                    )
                    if self.cluster_autoscaler is not None
                    else None
                ),
                "workload_cluster_ready": self.workload_cluster_ready,
            }
        )
