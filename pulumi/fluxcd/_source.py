# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Flux CD plumbing for GitRepository sources and webhook receivers."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence

import pulumi
import pulumi_kubernetes as k8s
import pulumi_random as random
from pulumi import Output, ResourceOptions

from stacks.kubernetes_annotations import pulumi_wait_for


FLUX_NAMESPACE = "flux-system"
FLUX_CHART_OCI = "oci://ghcr.io/fluxcd-community/charts/flux2"
FLUX_CHART_VERSION = "2.18.4"
FLUX_SOURCE_API_VERSION = "source.toolkit.fluxcd.io/v1"
FLUX_SOURCE_KIND = "GitRepository"
FLUX_SOURCE_NAME = "gitops-source"
FLUX_GIT_AUTH_SECRET_NAME = "gitops-source-ssh"
FLUX_RECEIVER_API_VERSION = "notification.toolkit.fluxcd.io/v1"
FLUX_RECEIVER_KIND = "Receiver"
FLUX_RECEIVER_NAME = "gitops-source"
FLUX_RECEIVER_TOKEN_SECRET_NAME = "gitops-source-webhook-token"
_BOOTSTRAP_HELM_TIMEOUT_SECONDS = 30 * 60
_BOOTSTRAP_HELM_TIMEOUT = "30m"


def _secret_data(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _receiver_path(token: str, name: str, namespace: str) -> str:
    digest = hashlib.sha256(f"{token}{name}{namespace}".encode("utf-8")).hexdigest()
    return f"/hook/{digest}"


def _wait_for_artifact_revision(branch: str, revision: str) -> dict[str, str]:
    return pulumi_wait_for(
        f"jsonpath={{.status.artifact.revision}}={branch}@sha1:{revision}"
    )


class FluxInfrastructure(pulumi.ComponentResource):
    """Install the minimal Flux controllers needed for source artifacts."""

    namespace: Output[str]
    release_status: Output[object]

    def __init__(
        self,
        name: str,
        *,
        provider: k8s.Provider,
        artifact_consumer_namespaces: Sequence[pulumi.Input[str]] = (),
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:fluxcd:FluxInfrastructure", name, props={}, opts=opts)

        flux_ns = k8s.core.v1.Namespace(
            f"{name}-ns",
            metadata={"name": FLUX_NAMESPACE},
            opts=ResourceOptions(parent=self, provider=provider),
        )

        release = k8s.helm.v3.Release(
            f"{name}-helm",
            chart=FLUX_CHART_OCI,
            version=FLUX_CHART_VERSION,
            namespace=FLUX_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=_BOOTSTRAP_HELM_TIMEOUT_SECONDS,
            values={
                "installCRDs": True,
                "helmController": {"create": False},
                "imageAutomationController": {"create": False},
                "imageReflectionController": {"create": False},
                "kustomizeController": {"create": False},
                "notificationController": {"create": True},
                "sourceController": {"create": True},
            },
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[flux_ns],
                custom_timeouts=pulumi.CustomTimeouts(
                    create=_BOOTSTRAP_HELM_TIMEOUT,
                    update=_BOOTSTRAP_HELM_TIMEOUT,
                    delete=_BOOTSTRAP_HELM_TIMEOUT,
                ),
            ),
        )

        artifact_ingress = None
        if artifact_consumer_namespaces:
            artifact_ingress = k8s.networking.v1.NetworkPolicy(
                f"{name}-source-artifact-ingress",
                metadata={
                    "name": "allow-source-controller-artifacts-from-consumers",
                    "namespace": FLUX_NAMESPACE,
                },
                spec={
                    "pod_selector": {"match_labels": {"app": "source-controller"}},
                    "policy_types": ["Ingress"],
                    "ingress": [
                        {
                            "from_": [
                                {
                                    "namespace_selector": {
                                        "match_labels": {
                                            "kubernetes.io/metadata.name": namespace
                                        }
                                    }
                                }
                                for namespace in artifact_consumer_namespaces
                            ],
                            "ports": [{"protocol": "TCP", "port": 9090}],
                        }
                    ],
                },
                opts=ResourceOptions(
                    parent=self,
                    provider=provider,
                    depends_on=[release],
                ),
            )

        self.namespace = Output.from_input(FLUX_NAMESPACE)
        self.release_status = release.status

        self.register_outputs(
            {
                "namespace": self.namespace,
                "release_status": self.release_status,
                "artifact_ingress_policy": (
                    artifact_ingress.metadata["name"] if artifact_ingress else None
                ),
            }
        )


class FluxSource(pulumi.ComponentResource):
    """Declare the shared Flux GitRepository source and Receiver."""

    api_version: str
    kind: str
    namespace: Output[str]
    source_name: Output[str]
    receiver_token: Output[str]
    receiver_path: Output[str]
    receiver_url: Output[str]
    resource: pulumi.Resource

    def __init__(
        self,
        name: str,
        *,
        provider: k8s.Provider,
        namespace: pulumi.Input[str],
        repo_url: pulumi.Input[str],
        repo_branch: pulumi.Input[str],
        git_auth_secret_name: pulumi.Input[str],
        expected_revision: pulumi.Input[str] | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:fluxcd:FluxSource", name, props={}, opts=opts)

        self.api_version = FLUX_SOURCE_API_VERSION
        self.kind = FLUX_SOURCE_KIND
        child_opts = ResourceOptions.merge(
            opts,
            ResourceOptions(parent=self, provider=provider),
        )
        git_repository_annotations = None
        if expected_revision is not None:
            git_repository_annotations = Output.all(repo_branch, expected_revision).apply(
                lambda args: _wait_for_artifact_revision(args[0], args[1])
            )
        git_repository_metadata: dict[str, pulumi.Input[object]] = {
            "name": FLUX_SOURCE_NAME,
            "namespace": namespace,
        }
        if git_repository_annotations is not None:
            git_repository_metadata["annotations"] = git_repository_annotations

        git_repository = k8s.apiextensions.CustomResource(
            f"{name}-git-repository",
            api_version=self.api_version,
            kind=self.kind,
            metadata=git_repository_metadata,
            spec={
                "interval": "30s",
                "url": repo_url,
                "ref": {"branch": repo_branch},
                "secretRef": {"name": git_auth_secret_name},
                "timeout": "2m0s",
            },
            opts=child_opts,
        )

        receiver_token = random.RandomPassword(
            f"{name}-receiver-token",
            length=32,
            special=False,
            opts=ResourceOptions(parent=self),
        )
        receiver_token_secret = k8s.core.v1.Secret(
            f"{name}-receiver-token-secret",
            metadata={
                "name": FLUX_RECEIVER_TOKEN_SECRET_NAME,
                "namespace": namespace,
                "labels": {"reconcile.fluxcd.io/watch": "Enabled"},
            },
            type="Opaque",
            data={"token": receiver_token.result.apply(_secret_data)},
            opts=ResourceOptions(
                parent=self,
                provider=provider,
            ),
        )
        receiver = k8s.apiextensions.CustomResource(
            f"{name}-receiver",
            api_version=FLUX_RECEIVER_API_VERSION,
            kind=FLUX_RECEIVER_KIND,
            metadata={
                "name": FLUX_RECEIVER_NAME,
                "namespace": namespace,
            },
            spec={
                "type": "github",
                "events": ["ping", "push"],
                "secretRef": {"name": FLUX_RECEIVER_TOKEN_SECRET_NAME},
                "resources": [
                    {
                        "apiVersion": FLUX_SOURCE_API_VERSION,
                        "kind": FLUX_SOURCE_KIND,
                        "name": FLUX_SOURCE_NAME,
                    }
                ],
            },
            opts=ResourceOptions.merge(
                opts,
                ResourceOptions(
                    parent=self,
                    provider=provider,
                    depends_on=[receiver_token_secret, git_repository],
                ),
            ),
        )

        self.namespace = Output.from_input(namespace)
        self.source_name = Output.from_input(FLUX_SOURCE_NAME)
        self.receiver_token = Output.secret(receiver_token.result)
        self.receiver_path = Output.all(receiver_token.result, self.namespace).apply(
            lambda args: _receiver_path(args[0], FLUX_RECEIVER_NAME, args[1])
        )
        self.receiver_url = Output.concat(
            "http://webhook-receiver.",
            FLUX_NAMESPACE,
            ".svc.cluster.local",
            self.receiver_path,
        )
        self.resource = git_repository

        self.register_outputs(
            {
                "api_version": self.api_version,
                "kind": self.kind,
                "namespace": self.namespace,
                "source_name": self.source_name,
                "receiver_token": self.receiver_token,
                "receiver_path": self.receiver_path,
                "receiver_url": self.receiver_url,
                "git_repository": git_repository.metadata["name"],  # type: ignore[attr-defined]
                "receiver": receiver.metadata["name"],  # type: ignore[attr-defined]
            }
        )