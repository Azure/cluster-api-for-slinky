from __future__ import annotations

import atexit
import asyncio

import pulumi

from stacks.init.init_stack import (
    INIT_STACK_CONFIG_KEY,
    InitStackConfig,
)
from stacks.control_plane.control_plane_config import (
    AzureInfrastructureProviderConfig,
    ControlPlaneAWXConfig,
    ControlPlaneDeploymentsConfig,
    ControlPlaneKindConfig,
    InfrastructureProvidersConfig,
    UserAssignedMSIClusterIdentityConfig,
)
from pko.pko_bootstrap import _init_stack_config_with_flux_source
from stacks.workload_cluster.registry_setting import LocalPortRegistrySetting
from stacks.workload_cluster.workload_cluster_class_local import LocalWorkloadClusterConfig
from stacks.workload_cluster.tenants import TenantsConfig
from stacks.stack_cr import StackCRConfig, build_stack_spec


_BASE_EVENT_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_BASE_EVENT_LOOP)
atexit.register(_BASE_EVENT_LOOP.close)


def _stack_spec() -> StackCRConfig:
    return StackCRConfig(
        pko_namespace="pulumi-kubernetes-operator",
        service_account_name="pulumi-runner",
        flux_source_name="gitops-source",
        flux_source_namespace="gitea",
        state_pvc_name="pko-state",
        state_backend_url="file:///state",
        passphrase_secret_name="pko-state-passphrase",
    )


def test_init_stack_config_serializes_tenants() -> None:
    config = InitStackConfig(
        tenants=TenantsConfig(
            workload_clusters={
                "local": LocalWorkloadClusterConfig(
                    registry=LocalPortRegistrySetting(port=5002),
                )
            }
        ),
    )

    assert config.to_config() == {
        "tenants": {
            "workloadClusters": {
                "local": {
                    "className": "local",
                    "registry": {"kind": "local-port", "port": 5002},
                }
            }
        },
    }


def test_init_stack_config_serializes_azure_uuid_fields_as_strings() -> None:
    config = InitStackConfig(
        control_plane=ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    identity=UserAssignedMSIClusterIdentityConfig(
                        client_id="11111111-1111-1111-1111-111111111111",
                        tenant_id="22222222-2222-2222-2222-222222222222",
                    ),
                    default_subscription_id="33333333-3333-3333-3333-333333333333",
                    default_location="westus2",
                    default_resource_group="ZHEYUSWESTUS2",
                )
            )
        )
    )

    assert config.to_config()["controlPlane"] == {
        "infrastructureProviders": {
            "azure": {
                "enabled": True,
                "identity": {
                    "type": "UserAssignedMSI",
                    "clientId": "11111111-1111-1111-1111-111111111111",
                    "tenantId": "22222222-2222-2222-2222-222222222222",
                },
                "defaultSubscriptionId": "33333333-3333-3333-3333-333333333333",
                "defaultLocation": "westus2",
                "defaultResourceGroup": "ZHEYUSWESTUS2",
            }
        }
    }


def test_output_init_stack_config_serializes_to_config() -> None:
    previous_loop = asyncio.get_event_loop()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        config = _init_stack_config_with_flux_source(
            init_stack_config=InitStackConfig(
                control_plane=ControlPlaneKindConfig(
                    deployments=ControlPlaneDeploymentsConfig(
                        awx=ControlPlaneAWXConfig(enabled=True)
                    )
                )
            ),
            flux_source_name=pulumi.Output.from_input("gitops-source"),
            flux_source_namespace="gitea",
        )

        assert isinstance(config, pulumi.Output)
        payload = pulumi.Output.from_input(config).apply(lambda resolved: resolved.to_config())
        assert isinstance(payload, pulumi.Output)
        assert loop.run_until_complete(payload.future()) == {
            "controlPlane": {
                "deployments": {
                    "awx": {
                        "enabled": True,
                        "fluxSourceName": "gitops-source",
                        "fluxSourceNamespace": "gitea",
                    }
                }
            }
        }
    finally:
        asyncio.set_event_loop(previous_loop)
        loop.close()


def test_output_init_stack_config_accepts_output_base_config() -> None:
    previous_loop = asyncio.get_event_loop()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        config = _init_stack_config_with_flux_source(
            init_stack_config=pulumi.Output.from_input(
                InitStackConfig(
                    control_plane=ControlPlaneKindConfig(
                        infrastructure_providers=InfrastructureProvidersConfig(
                            azure=AzureInfrastructureProviderConfig(
                                enabled=True,
                                identity=UserAssignedMSIClusterIdentityConfig(
                                    client_id="11111111-1111-1111-1111-111111111111",
                                    tenant_id="22222222-2222-2222-2222-222222222222",
                                ),
                                provider_oci="http://custom-registry/capz:tag",
                                controller_image="custom-registry:5000/capz/controller:tag",
                            )
                        )
                    )
                )
            ),
            flux_source_name="gitops-source",
            flux_source_namespace="gitea",
        )

        assert isinstance(config, pulumi.Output)
        resolved = loop.run_until_complete(config.future())
        assert resolved.control_plane.infrastructure_providers.azure.provider_oci == (
            "http://custom-registry/capz:tag"
        )
        assert resolved.control_plane.infrastructure_providers.azure.controller_image == (
            "custom-registry:5000/capz/controller:tag"
        )
    finally:
        asyncio.set_event_loop(previous_loop)
        loop.close()


def test_build_stack_spec_uses_flux_source_namespace() -> None:
    spec = build_stack_spec(
        spec=_stack_spec(),
        project_name="ca4s-init",
        env="local",
        repo_dir="pulumi/stacks/init",
    )

    assert spec["fluxSource"]["sourceRef"] == {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "GitRepository",
        "name": "gitops-source",
    }
    assert spec["envRefs"]["PULUMI_HOME"] == {
        "type": "Literal",
        "literal": {"value": "/share/.pulumi"},
    }
    assert spec["envRefs"]["HOME"] == {
        "type": "Literal",
        "literal": {"value": "/share"},
    }
    assert spec["envRefs"]["USER"] == {
        "type": "Literal",
        "literal": {"value": "pulumi"},
    }
    assert spec["envRefs"]["PYTHONPATH"] == {
        "type": "Literal",
        "literal": {"value": "/share/source/pulumi"},
    }
    assert "PULUMI_K8S_DELETE_UNREACHABLE" not in spec["envRefs"]
    assert spec["updateTemplate"] == {
        "spec": {"ttlAfterCompleted": "1m"},
    }
    assert spec["destroyOnFinalize"] is True
    assert spec["workspaceTemplate"]["spec"]["podTemplate"]["spec"]["containers"] == [
        {
            "name": "pulumi",
            "image": "pulumi/pulumi-python:3.202.0",
            "volumeMounts": [{"name": "state", "mountPath": "/state"}],
        }
    ]


def test_build_stack_spec_accepts_output_stack_cr_config() -> None:
    previous_loop = asyncio.get_event_loop()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        spec = build_stack_spec(
            spec=pulumi.Output.from_input(_stack_spec()),
            project_name="ca4s-init",
            env="local",
            repo_dir="pulumi/stacks/init",
        )

        assert isinstance(spec, pulumi.Output)
        resolved = loop.run_until_complete(spec.future())
        assert resolved["fluxSource"]["sourceRef"] == {
            "apiVersion": "source.toolkit.fluxcd.io/v1",
            "kind": "GitRepository",
            "name": "gitops-source",
        }
        assert resolved["serviceAccountName"] == "pulumi-runner"
        assert resolved["backend"] == "file:///state"
    finally:
        asyncio.set_event_loop(previous_loop)
        loop.close()
