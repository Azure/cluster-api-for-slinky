from __future__ import annotations

import asyncio

import pulumi
import pytest

from stacks.init.init_stack import (
    INIT_STACK_CONFIG_KEY,
    INIT_STACK_SPEC_CONFIG_KEY,
    InitStackConfig,
    init_stack_config,
    parse_init_stack_spec,
)
from stacks.control_plane.control_plane_config import (
    AzureInfrastructureProviderConfig,
    ControlPlaneKindConfig,
    InfrastructureProvidersConfig,
    UserAssignedMSIClusterIdentityConfig,
)
from stacks.workload_cluster.registry_setting import LocalPortRegistrySetting
from stacks.workload_cluster.workload_cluster_class_local import LocalWorkloadClusterConfig
from stacks.workload_cluster.tenants import TenantsConfig
from stacks.stack_cr import StackCRConfig, build_stack_spec


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


def test_init_stack_config_wraps_shared_spec_and_init_stack_config() -> None:
    config = InitStackConfig(
        tenants=TenantsConfig(
            workload_clusters={
                "local": LocalWorkloadClusterConfig(
                    registry=LocalPortRegistrySetting(port=5002),
                )
            }
        ),
    )

    payload = init_stack_config(
        stack_spec=_stack_spec(),
        init_stack_config=config,
    )

    assert payload[INIT_STACK_SPEC_CONFIG_KEY] == {
        "pkoNamespace": "pulumi-kubernetes-operator",
        "serviceAccountName": "pulumi-runner",
        "fluxSourceName": "gitops-source",
        "fluxSourceNamespace": "gitea",
        "statePvcName": "pko-state",
        "stateBackendUrl": "file:///state",
        "passphraseSecretName": "pko-state-passphrase",
    }
    assert payload[INIT_STACK_CONFIG_KEY] == {
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

    payload = init_stack_config(
        stack_spec=_stack_spec(),
        init_stack_config=config,
    )

    assert payload[INIT_STACK_CONFIG_KEY]["controlPlane"] == {
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


def test_parse_init_stack_spec_round_trips_config_payload() -> None:
    payload = init_stack_config(stack_spec=_stack_spec())[INIT_STACK_SPEC_CONFIG_KEY]

    parsed = parse_init_stack_spec(payload)

    assert parsed == _stack_spec()


def test_stack_spec_config_resolves_pulumi_outputs() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        payload = StackCRConfig(
            pko_namespace=pulumi.Output.from_input("pulumi-kubernetes-operator"),
            service_account_name="pulumi-runner",
            flux_source_name="gitops-source",
            flux_source_namespace="gitea",
            state_pvc_name="pko-state",
            state_backend_url="file:///state",
            passphrase_secret_name="pko-state-passphrase",
        ).to_config()

        assert isinstance(payload, pulumi.Output)
        assert loop.run_until_complete(payload.future()) == {
            "pkoNamespace": "pulumi-kubernetes-operator",
            "serviceAccountName": "pulumi-runner",
            "fluxSourceName": "gitops-source",
            "fluxSourceNamespace": "gitea",
            "statePvcName": "pko-state",
            "stateBackendUrl": "file:///state",
            "passphraseSecretName": "pko-state-passphrase",
        }
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())
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
    assert spec["workspaceTemplate"]["spec"]["podTemplate"]["spec"]["containers"] == [
        {
            "name": "pulumi",
            "image": "pulumi/pulumi-python:3.202.0",
            "volumeMounts": [{"name": "state", "mountPath": "/state"}],
        }
    ]


def test_parse_init_stack_spec_rejects_missing_required_field() -> None:
    payload = dict(init_stack_config(stack_spec=_stack_spec())[INIT_STACK_SPEC_CONFIG_KEY])
    del payload["fluxSourceName"]

    with pytest.raises(ValueError, match=f"{INIT_STACK_SPEC_CONFIG_KEY}.fluxSourceName"):
        parse_init_stack_spec(payload)
