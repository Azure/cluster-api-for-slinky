"""ESO-managed Flux SSH auth Secret for Kubernetes-generated git keys."""

from __future__ import annotations

from typing import Optional

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions


_STORE_NAME = "gitea-ssh-key-store"
_STORE_SERVICE_ACCOUNT = "gitea-ssh-key-reader"


class FluxGitAuthSecret(pulumi.ComponentResource):
    """Creates Flux's git auth Secret with ESO/Kubernetes resources only."""

    name: Output[str]
    namespace: Output[str]

    def __init__(
        self,
        name: str,
        *,
        provider: k8s.Provider,
        source_namespace: pulumi.Input[str],
        user_secret_name: pulumi.Input[str],
        user_private_key_key: pulumi.Input[str],
        host_secret_name: pulumi.Input[str],
        host_public_key_key: pulumi.Input[str],
        target_namespace: pulumi.Input[str],
        target_name: pulumi.Input[str],
        known_hosts_hostname: pulumi.Input[str],
        opts: Optional[pulumi.ResourceOptions] = None,
    ) -> None:
        super().__init__("ca4s:gitrepo:FluxGitAuthSecret", name, props={}, opts=opts)

        service_account = k8s.core.v1.ServiceAccount(
            f"{name}-reader-sa",
            metadata={"name": _STORE_SERVICE_ACCOUNT, "namespace": target_namespace},
            opts=ResourceOptions(parent=self, provider=provider),
        )

        role = k8s.rbac.v1.Role(
            f"{name}-reader-role",
            metadata={"name": _STORE_SERVICE_ACCOUNT, "namespace": source_namespace},
            rules=[
                {
                    "api_groups": [""],
                    "resources": ["secrets"],
                    "verbs": ["get", "list", "watch"],
                },
                {
                    "api_groups": ["authorization.k8s.io"],
                    "resources": ["selfsubjectrulesreviews"],
                    "verbs": ["create"],
                },
            ],
            opts=ResourceOptions(parent=self, provider=provider),
        )

        role_binding = k8s.rbac.v1.RoleBinding(
            f"{name}-reader-rolebinding",
            metadata={"name": _STORE_SERVICE_ACCOUNT, "namespace": source_namespace},
            role_ref={
                "api_group": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": role.metadata["name"],
            },
            subjects=[
                {
                    "kind": "ServiceAccount",
                    "name": service_account.metadata["name"],
                    "namespace": target_namespace,
                }
            ],
            opts=ResourceOptions(parent=self, provider=provider, depends_on=[role]),
        )

        store = k8s.apiextensions.CustomResource(
            f"{name}-store",
            api_version="external-secrets.io/v1",
            kind="SecretStore",
            metadata={"name": _STORE_NAME, "namespace": target_namespace},
            spec={
                "provider": {
                    "kubernetes": {
                        "remoteNamespace": source_namespace,
                        "server": {
                            "caProvider": {
                                "type": "ConfigMap",
                                "name": "kube-root-ca.crt",
                                "key": "ca.crt",
                            }
                        },
                        "auth": {
                            "serviceAccount": {
                                "name": service_account.metadata["name"],
                                "namespace": target_namespace,
                            }
                        },
                    }
                }
            },
            opts=ResourceOptions(parent=self, provider=provider, depends_on=[role_binding]),
        )

        auth_secret = k8s.apiextensions.CustomResource(
            f"{name}-external-secret",
            api_version="external-secrets.io/v1",
            kind="ExternalSecret",
            metadata={"name": target_name, "namespace": target_namespace},
            spec={
                "refreshPolicy": "CreatedOnce",
                "secretStoreRef": {"kind": "SecretStore", "name": _STORE_NAME},
                "target": {
                    "name": target_name,
                    "creationPolicy": "Owner",
                    "template": {
                        "engineVersion": "v2",
                        "type": "Opaque",
                        "metadata": {
                            "labels": {
                                "app.kubernetes.io/managed-by": "external-secrets"
                            }
                        },
                        "data": {
                            "identity": "{{ .identity }}",
                            "known_hosts": Output.concat(
                                known_hosts_hostname,
                                " {{ .hostPublicKey }}\n",
                            ),
                        },
                    },
                },
                "data": [
                    {
                        "secretKey": "identity",
                        "remoteRef": {
                            "key": user_secret_name,
                            "property": user_private_key_key,
                        },
                    },
                    {
                        "secretKey": "hostPublicKey",
                        "remoteRef": {
                            "key": host_secret_name,
                            "property": host_public_key_key,
                        },
                    },
                ],
            },
            opts=ResourceOptions(parent=self, provider=provider, depends_on=[store]),
        )

        self.name = Output.from_input(target_name)
        self.namespace = Output.from_input(target_namespace)

        self.register_outputs(
            {
                "name": self.name,
                "namespace": self.namespace,
                "secret_store": store.metadata["name"],  # type: ignore[index]
                "external_secret": auth_secret.metadata["name"],  # type: ignore[index]
            }
        )