"""In-cluster reconciler that keeps selected ASO resources detached on delete.

This is a workaround for CAPZ managed AKS teardown: CAPZ skips direct agent-pool
deletion when the owning CAPI Cluster is deleting, but its generated ASO
ManagedClustersAgentPool can still try the child Azure delete unless its ASO
reconcile policy is reset to detach-on-delete. The root-cause fix is tracked in
CAPZ PR https://github.com/kubernetes-sigs/cluster-api-provider-azure/pull/6447.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pulumi
import pulumi_kubernetes as k8s
from pulumi import ResourceOptions

from stacks.kubernetes_annotations import (
    ASO_RECONCILE_POLICY_ANNOTATION,
    ASO_RECONCILE_POLICY_DETACH_ON_DELETE,
)


ASO_DETACH_ON_DELETE_LABEL = "ca4s.azure.com/aso-detach-on-delete"
ASO_DETACH_ON_DELETE_CLUSTER_LABEL = "ca4s.azure.com/cluster"

_CONFIG_MAP_NAME_SUFFIX = "aso-detach-reconciler"
_SCRIPT_NAME = "reconcile.py"
_SERVICE_ACCOUNT_NAME_SUFFIX = "aso-detach-reconciler"
_RBAC_NAME_SUFFIX = "aso-detach-reconciler"
_DEPLOYMENT_NAME_SUFFIX = "aso-detach-reconciler"
_RECONCILER_IMAGE = "python:3.13-alpine"
_WATCH_TIMEOUT_SECONDS = 300
_WATCH_RETRY_SECONDS = 5


def aso_agent_pool_detach_label_patch(*, cluster_name: str) -> str:
    return json.dumps(
        {
            "metadata": {
                "labels": {
                    ASO_DETACH_ON_DELETE_LABEL: "true",
                    ASO_DETACH_ON_DELETE_CLUSTER_LABEL: cluster_name,
                },
            },
        },
        sort_keys=True,
    )


def aso_detach_reconciler_label_selector(*, cluster_name: str) -> str:
    return (
        f"{ASO_DETACH_ON_DELETE_LABEL}=true,"
        f"{ASO_DETACH_ON_DELETE_CLUSTER_LABEL}={cluster_name}"
    )


def aso_detach_reconciler_script() -> str:
    return f'''\
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


API_GROUP = "containerservice.azure.com"
API_VERSION = "v1api20231001"
PLURAL = "managedclustersagentpools"
ANNOTATION = "{ASO_RECONCILE_POLICY_ANNOTATION}"
DETACH_ON_DELETE = "{ASO_RECONCILE_POLICY_DETACH_ON_DELETE}"
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
WATCH_TIMEOUT_SECONDS = {_WATCH_TIMEOUT_SECONDS}
WATCH_RETRY_SECONDS = {_WATCH_RETRY_SECONDS}


def log(message):
    print(message, flush=True)


def api_base():
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    return f"https://{{host}}:{{port}}"


def ssl_context():
    return ssl.create_default_context(cafile=CA_PATH)


def token():
    with open(TOKEN_PATH, encoding="utf-8") as token_file:
        return token_file.read().strip()


def request(method, path, body=None, content_type=None, timeout=60):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {{"Authorization": f"Bearer {{token()}}"}}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(
        f"{{api_base()}}{{path}}",
        data=data,
        headers=headers,
        method=method,
    )
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())


def resource_path(namespace, name=None, **query):
    path = f"/apis/{{API_GROUP}}/{{API_VERSION}}/namespaces/{{namespace}}/{{PLURAL}}"
    if name:
        path = f"{{path}}/{{name}}"
    clean_query = {{key: value for key, value in query.items() if value is not None}}
    if clean_query:
        path = f"{{path}}?{{urllib.parse.urlencode(clean_query)}}"
    return path


def list_resources(namespace, label_selector):
    with request(
        "GET",
        resource_path(namespace, labelSelector=label_selector),
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def patch_detach_on_delete(resource):
    metadata = resource.get("metadata") or {{}}
    name = metadata.get("name")
    namespace = metadata.get("namespace")
    annotations = metadata.get("annotations") or {{}}
    if not name or not namespace:
        return
    if annotations.get(ANNOTATION) == DETACH_ON_DELETE:
        return
    patch = {{"metadata": {{"annotations": {{ANNOTATION: DETACH_ON_DELETE}}}}}}
    with request(
        "PATCH",
        resource_path(namespace, name),
        body=patch,
        content_type="application/merge-patch+json",
    ):
        pass
    log(f"set {{ANNOTATION}}={{DETACH_ON_DELETE}} on {{namespace}}/{{name}}")


def reconcile_existing(namespace, label_selector):
    payload = list_resources(namespace, label_selector)
    for resource in payload.get("items", []):
        patch_detach_on_delete(resource)
    return payload.get("metadata", {{}}).get("resourceVersion")


def watch(namespace, label_selector, resource_version):
    with request(
        "GET",
        resource_path(
            namespace,
            labelSelector=label_selector,
            resourceVersion=resource_version,
            timeoutSeconds=str(WATCH_TIMEOUT_SECONDS),
            watch="true",
        ),
        timeout=WATCH_TIMEOUT_SECONDS + 30,
    ) as response:
        for line in response:
            if not line.strip():
                continue
            event = json.loads(line.decode("utf-8"))
            if event.get("type") in ("ADDED", "MODIFIED"):
                patch_detach_on_delete(event.get("object") or {{}})


def main():
    namespace = os.environ["WATCH_NAMESPACE"]
    label_selector = os.environ["LABEL_SELECTOR"]
    log(f"watching {{namespace}} {{PLURAL}} with selector {{label_selector}}")
    resource_version = None
    while True:
        try:
            resource_version = reconcile_existing(namespace, label_selector)
            watch(namespace, label_selector, resource_version)
        except urllib.error.HTTPError as exc:
            if exc.code == 410:
                resource_version = None
            else:
                log(f"Kubernetes API error: {{exc}}"); time.sleep(WATCH_RETRY_SECONDS)
        except Exception as exc:
            log(f"watch failed: {{exc}}"); time.sleep(WATCH_RETRY_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
'''


class ASODetachReconciler(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        *,
        namespace: str,
        cluster_name: str,
        provider: k8s.Provider | None = None,
        depends_on: Sequence[pulumi.Input[pulumi.Resource]] | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:workload:ASODetachReconciler", name, props={}, opts=opts)

        resource_name = f"{cluster_name}-{_DEPLOYMENT_NAME_SUFFIX}"
        selector_labels = {"app.kubernetes.io/name": resource_name}

        def child_opts(
            child_depends_on: Sequence[pulumi.Input[pulumi.Resource]] | None = None,
        ) -> ResourceOptions:
            return ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=child_depends_on,
            )

        service_account = k8s.core.v1.ServiceAccount(
            f"{name}-service-account",
            metadata={
                "name": f"{cluster_name}-{_SERVICE_ACCOUNT_NAME_SUFFIX}",
                "namespace": namespace,
            },
            opts=child_opts(depends_on),
        )
        role = k8s.rbac.v1.Role(
            f"{name}-role",
            metadata={
                "name": f"{cluster_name}-{_RBAC_NAME_SUFFIX}",
                "namespace": namespace,
            },
            rules=[
                k8s.rbac.v1.PolicyRuleArgs(
                    api_groups=["containerservice.azure.com"],
                    resources=["managedclustersagentpools"],
                    verbs=["get", "list", "watch", "patch"],
                )
            ],
            opts=child_opts(depends_on),
        )
        role_binding = k8s.rbac.v1.RoleBinding(
            f"{name}-role-binding",
            metadata={
                "name": f"{cluster_name}-{_RBAC_NAME_SUFFIX}",
                "namespace": namespace,
            },
            role_ref=k8s.rbac.v1.RoleRefArgs(
                api_group="rbac.authorization.k8s.io",
                kind="Role",
                name=f"{cluster_name}-{_RBAC_NAME_SUFFIX}",
            ),
            subjects=[
                k8s.rbac.v1.SubjectArgs(
                    kind="ServiceAccount",
                    name=f"{cluster_name}-{_SERVICE_ACCOUNT_NAME_SUFFIX}",
                    namespace=namespace,
                )
            ],
            opts=child_opts([role, service_account]),
        )
        config = k8s.core.v1.ConfigMap(
            f"{name}-config",
            metadata={
                "name": f"{cluster_name}-{_CONFIG_MAP_NAME_SUFFIX}",
                "namespace": namespace,
            },
            data={_SCRIPT_NAME: aso_detach_reconciler_script()},
            opts=child_opts(depends_on),
        )
        deployment = k8s.apps.v1.Deployment(
            f"{name}-deployment",
            metadata={
                "name": resource_name,
                "namespace": namespace,
                "labels": selector_labels,
            },
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=selector_labels),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=selector_labels),
                    spec=k8s.core.v1.PodSpecArgs(
                        service_account_name=f"{cluster_name}-{_SERVICE_ACCOUNT_NAME_SUFFIX}",
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="reconciler",
                                image=_RECONCILER_IMAGE,
                                command=["python", f"/scripts/{_SCRIPT_NAME}"],
                                env=[
                                    k8s.core.v1.EnvVarArgs(
                                        name="WATCH_NAMESPACE",
                                        value=namespace,
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="LABEL_SELECTOR",
                                        value=aso_detach_reconciler_label_selector(
                                            cluster_name=cluster_name,
                                        ),
                                    ),
                                ],
                                volume_mounts=[
                                    k8s.core.v1.VolumeMountArgs(
                                        name="script",
                                        mount_path="/scripts",
                                        read_only=True,
                                    )
                                ],
                            )
                        ],
                        volumes=[
                            k8s.core.v1.VolumeArgs(
                                name="script",
                                config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(
                                    name=f"{cluster_name}-{_CONFIG_MAP_NAME_SUFFIX}",
                                ),
                            )
                        ],
                    ),
                ),
            ),
            opts=child_opts([config, role_binding, service_account]),
        )

        self.deployment = deployment
        self.register_outputs({"deployment": deployment.metadata["name"]})


__all__ = [
    "ASO_DETACH_ON_DELETE_CLUSTER_LABEL",
    "ASO_DETACH_ON_DELETE_LABEL",
    "ASODetachReconciler",
    "aso_agent_pool_detach_label_patch",
    "aso_detach_reconciler_label_selector",
    "aso_detach_reconciler_script",
]