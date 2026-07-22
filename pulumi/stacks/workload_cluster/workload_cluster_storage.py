"""Temporary local-node storage for self-managed workload clusters."""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s

from stacks.workload_cluster.workload_cluster_infrastructure import (
    POD_SECURITY_PRIVILEGED_LABELS,
    controller_node_affinity,
    controller_tolerations,
)


_LOCAL_PATH_NAMESPACE = "local-path-storage"
_LOCAL_PATH_STORAGE_CLASS = "local-path"
_LOCAL_PATH_PROVISIONER_VERSION = "v0.0.32"
_LOCAL_PATH_SERVICE_ACCOUNT = "local-path-provisioner-service-account"
_LOCAL_PATH_RBAC_NAME = "local-path-provisioner-role"
_LOCAL_PATH_RBAC_BINDING_NAME = "local-path-provisioner-bind"
_LOCAL_PATH_CONFIG_NAME = "local-path-config"
_LOCAL_PATH_DEPLOYMENT_NAME = "local-path-provisioner"


def _local_path_config_data() -> dict[str, str]:
    return {
        "config.json": (
            '{\n  "nodePathMap":[{\n'
            '    "node":"DEFAULT_PATH_FOR_NON_LISTED_NODES",\n'
            '    "paths":["/opt/local-path-provisioner"]\n  }]\n}'
        ),
        "setup": '#!/bin/sh\nset -eu\nmkdir -m 0777 -p "$VOL_DIR"\n',
        "teardown": '#!/bin/sh\nset -eu\nrm -rf "$VOL_DIR"\n',
        "helperPod.yaml": (
            "apiVersion: v1\n"
            "kind: Pod\n"
            "metadata:\n"
            "  name: helper-pod\n"
            "spec:\n"
            "  priorityClassName: system-node-critical\n"
            "  tolerations:\n"
            "    - key: node.kubernetes.io/disk-pressure\n"
            "      operator: Exists\n"
            "      effect: NoSchedule\n"
            "  containers:\n"
            "  - name: helper-pod\n"
            "    image: busybox\n"
            "    imagePullPolicy: IfNotPresent\n"
        ),
    }


class LocalPathStorage(pulumi.ComponentResource):
    """Install a stopgap node-local provisioner and default StorageClass."""

    namespace: pulumi.Output[str]
    storage_class_name: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        provider: k8s.Provider,
        depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:workload:LocalPathStorage", name, props={}, opts=opts)

        def child_options(
            *, depends_on: list[pulumi.Input[pulumi.Resource]] | None = None
        ) -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
            )

        namespace = k8s.core.v1.Namespace(
            "local-path-storage-namespace",
            metadata={
                "name": _LOCAL_PATH_NAMESPACE,
                "labels": POD_SECURITY_PRIVILEGED_LABELS,
            },
            opts=child_options(depends_on=depends_on),
        )
        service_account = k8s.core.v1.ServiceAccount(
            "local-path-service-account",
            metadata={
                "name": _LOCAL_PATH_SERVICE_ACCOUNT,
                "namespace": _LOCAL_PATH_NAMESPACE,
            },
            opts=child_options(depends_on=[namespace]),
        )
        role = k8s.rbac.v1.Role(
            "local-path-role",
            metadata={
                "name": _LOCAL_PATH_RBAC_NAME,
                "namespace": _LOCAL_PATH_NAMESPACE,
            },
            rules=[
                k8s.rbac.v1.PolicyRuleArgs(
                    api_groups=[""],
                    resources=["pods"],
                    verbs=[
                        "get",
                        "list",
                        "watch",
                        "create",
                        "patch",
                        "update",
                        "delete",
                    ],
                )
            ],
            opts=child_options(depends_on=[namespace]),
        )
        cluster_role = k8s.rbac.v1.ClusterRole(
            "local-path-cluster-role",
            metadata={"name": _LOCAL_PATH_RBAC_NAME},
            rules=[
                k8s.rbac.v1.PolicyRuleArgs(
                    api_groups=[""],
                    resources=[
                        "nodes",
                        "persistentvolumeclaims",
                        "configmaps",
                        "pods",
                        "pods/log",
                    ],
                    verbs=["get", "list", "watch"],
                ),
                k8s.rbac.v1.PolicyRuleArgs(
                    api_groups=[""],
                    resources=["persistentvolumes"],
                    verbs=[
                        "get",
                        "list",
                        "watch",
                        "create",
                        "patch",
                        "update",
                        "delete",
                    ],
                ),
                k8s.rbac.v1.PolicyRuleArgs(
                    api_groups=[""],
                    resources=["events"],
                    verbs=["create", "patch"],
                ),
                k8s.rbac.v1.PolicyRuleArgs(
                    api_groups=["storage.k8s.io"],
                    resources=["storageclasses"],
                    verbs=["get", "list", "watch"],
                ),
            ],
            opts=child_options(depends_on=depends_on),
        )
        k8s.rbac.v1.RoleBinding(
            "local-path-role-binding",
            metadata={
                "name": _LOCAL_PATH_RBAC_BINDING_NAME,
                "namespace": _LOCAL_PATH_NAMESPACE,
            },
            role_ref=k8s.rbac.v1.RoleRefArgs(
                api_group="rbac.authorization.k8s.io",
                kind="Role",
                name=_LOCAL_PATH_RBAC_NAME,
            ),
            subjects=[
                k8s.rbac.v1.SubjectArgs(
                    kind="ServiceAccount",
                    name=_LOCAL_PATH_SERVICE_ACCOUNT,
                    namespace=_LOCAL_PATH_NAMESPACE,
                )
            ],
            opts=child_options(depends_on=[role, service_account]),
        )
        k8s.rbac.v1.ClusterRoleBinding(
            "local-path-cluster-role-binding",
            metadata={"name": _LOCAL_PATH_RBAC_BINDING_NAME},
            role_ref=k8s.rbac.v1.RoleRefArgs(
                api_group="rbac.authorization.k8s.io",
                kind="ClusterRole",
                name=_LOCAL_PATH_RBAC_NAME,
            ),
            subjects=[
                k8s.rbac.v1.SubjectArgs(
                    kind="ServiceAccount",
                    name=_LOCAL_PATH_SERVICE_ACCOUNT,
                    namespace=_LOCAL_PATH_NAMESPACE,
                )
            ],
            opts=child_options(depends_on=[cluster_role, service_account]),
        )
        config = k8s.core.v1.ConfigMap(
            "local-path-config",
            metadata={
                "name": _LOCAL_PATH_CONFIG_NAME,
                "namespace": _LOCAL_PATH_NAMESPACE,
            },
            data=_local_path_config_data(),
            opts=child_options(depends_on=[namespace]),
        )
        storage_class = k8s.storage.v1.StorageClass(
            "local-path-storage-class",
            metadata={
                "name": _LOCAL_PATH_STORAGE_CLASS,
                "annotations": {
                    "storageclass.kubernetes.io/is-default-class": "true",
                    "defaultVolumeType": "local",
                },
            },
            provisioner="rancher.io/local-path",
            reclaim_policy="Delete",
            volume_binding_mode="WaitForFirstConsumer",
            opts=child_options(depends_on=depends_on),
        )
        k8s.apps.v1.Deployment(
            "local-path-deployment",
            metadata={
                "name": _LOCAL_PATH_DEPLOYMENT_NAME,
                "namespace": _LOCAL_PATH_NAMESPACE,
            },
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                selector=k8s.meta.v1.LabelSelectorArgs(
                    match_labels={"app": _LOCAL_PATH_DEPLOYMENT_NAME},
                ),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels={"app": _LOCAL_PATH_DEPLOYMENT_NAME},
                    ),
                    spec=k8s.core.v1.PodSpecArgs(
                        service_account_name=_LOCAL_PATH_SERVICE_ACCOUNT,
                        affinity=controller_node_affinity(),
                        tolerations=controller_tolerations(),
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name=_LOCAL_PATH_DEPLOYMENT_NAME,
                                image=(
                                    "rancher/local-path-provisioner:"
                                    f"{_LOCAL_PATH_PROVISIONER_VERSION}"
                                ),
                                image_pull_policy="IfNotPresent",
                                command=[
                                    "local-path-provisioner",
                                    "--debug",
                                    "start",
                                    "--config",
                                    "/etc/config/config.json",
                                ],
                                volume_mounts=[
                                    k8s.core.v1.VolumeMountArgs(
                                        name="config-volume",
                                        mount_path="/etc/config/",
                                    )
                                ],
                                env=[
                                    k8s.core.v1.EnvVarArgs(
                                        name="POD_NAMESPACE",
                                        value_from=k8s.core.v1.EnvVarSourceArgs(
                                            field_ref=k8s.core.v1.ObjectFieldSelectorArgs(
                                                field_path="metadata.namespace"
                                            ),
                                        ),
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="CONFIG_MOUNT_PATH",
                                        value="/etc/config/",
                                    ),
                                ],
                            )
                        ],
                        volumes=[
                            k8s.core.v1.VolumeArgs(
                                name="config-volume",
                                config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(
                                    name=_LOCAL_PATH_CONFIG_NAME,
                                ),
                            )
                        ],
                    ),
                ),
            ),
            opts=child_options(depends_on=[config, service_account, storage_class]),
        )

        self.namespace = pulumi.Output.from_input(_LOCAL_PATH_NAMESPACE)
        self.storage_class_name = pulumi.Output.from_input(_LOCAL_PATH_STORAGE_CLASS)
        self.register_outputs(
            {
                "namespace": self.namespace,
                "storage_class_name": self.storage_class_name,
            }
        )