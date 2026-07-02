from __future__ import annotations

import pytest

from stacks.init.init_stack import (
    INIT_CHILD_CONFIG_KEY,
    INIT_STACK_SPEC_CONFIG_KEY,
    init_stack_config,
    parse_init_stack_spec,
)
from stacks.stack_cr import StackCRSpec, build_stack_spec


def _stack_spec() -> StackCRSpec:
    return StackCRSpec(
        pko_namespace="pulumi-kubernetes-operator",
        service_account_name="pulumi-runner",
        flux_source_name="gitops-source",
        flux_source_namespace="gitea",
        state_pvc_name="pko-state",
        state_backend_url="file:///state",
        passphrase_secret_name="pko-state-passphrase",
    )


def test_init_stack_config_wraps_shared_spec_and_child_config() -> None:
    child_config = {
        "ca4s-workload-cluster:registry": {"kind": "local-port", "port": 5002},
    }

    config = init_stack_config(stack_spec=_stack_spec(), child_config=child_config)

    assert config[INIT_STACK_SPEC_CONFIG_KEY] == {
        "pkoNamespace": "pulumi-kubernetes-operator",
        "serviceAccountName": "pulumi-runner",
        "fluxSourceName": "gitops-source",
        "fluxSourceNamespace": "gitea",
        "statePvcName": "pko-state",
        "stateBackendUrl": "file:///state",
        "passphraseSecretName": "pko-state-passphrase",
    }
    assert config[INIT_CHILD_CONFIG_KEY] is child_config


def test_parse_init_stack_spec_round_trips_config_payload() -> None:
    payload = init_stack_config(stack_spec=_stack_spec())[INIT_STACK_SPEC_CONFIG_KEY]

    parsed = parse_init_stack_spec(payload)

    assert parsed == _stack_spec()


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
        "namespace": "gitea",
    }


def test_parse_init_stack_spec_rejects_missing_required_field() -> None:
    payload = dict(init_stack_config(stack_spec=_stack_spec())[INIT_STACK_SPEC_CONFIG_KEY])
    del payload["fluxSourceName"]

    with pytest.raises(ValueError, match=f"{INIT_STACK_SPEC_CONFIG_KEY}.fluxSourceName"):
        parse_init_stack_spec(payload)
