from __future__ import annotations

import base64

import pytest

from stacks.control_plane.awx import awx_api_url, decode_secret_data_value
from stacks.control_plane.awx._configuration import (
    dynamic_inventory_variables,
    flux_source_branch,
    flux_source_secret_name,
    flux_source_url,
    injectable_kubernetes_credential_type_injectors,
    injectable_kubernetes_credential_type_inputs,
    project_name_from_scm_url,
    management_kubernetes_credential_inputs,
    source_control_credential_inputs,
)
from stacks.control_plane.awx._project_sync import flux_artifact_revision_sha


def test_awx_api_url_uses_in_cluster_service_dns() -> None:
    assert (
        awx_api_url(namespace="awx", service_name="awx-service")
        == "http://awx-service.awx.svc.cluster.local"
    )


def test_decode_secret_data_value_decodes_base64_key() -> None:
    encoded = base64.b64encode(b"secret-password").decode("ascii")

    assert decode_secret_data_value({"password": encoded}, "password") == "secret-password"


@pytest.mark.parametrize("data", [None, {}, {"password": ""}])
def test_decode_secret_data_value_rejects_missing_key(
    data: dict[str, str] | None,
) -> None:
    with pytest.raises(KeyError, match=r"Secret data\['password'\] is missing"):
        decode_secret_data_value(data, "password")


@pytest.mark.parametrize(
    ("scm_url", "project_name"),
    [
        ("ssh://git@gitea-ssh.gitea.svc.cluster.local:22/caps/repo.git", "repo"),
        ("https://example.invalid/org/project", "project"),
        ("", "gitops"),
    ],
)
def test_project_name_from_scm_url(scm_url: str, project_name: str) -> None:
    assert project_name_from_scm_url(scm_url) == project_name


def test_flux_source_helpers_extract_project_inputs() -> None:
    spec = {
        "url": "ssh://git@gitea-ssh.gitea.svc.cluster.local:22/caps/repo.git",
        "ref": {"branch": "main"},
        "secret_ref": {"name": "gitops-source-ssh"},
    }

    assert flux_source_url(spec) == spec["url"]
    assert flux_source_branch(spec) == "main"
    assert flux_source_secret_name(spec) == "gitops-source-ssh"


@pytest.mark.parametrize(
    ("helper", "spec"),
    [
        (flux_source_url, {}),
        (flux_source_branch, {"ref": {}}),
        (flux_source_secret_name, {"secret_ref": {}}),
    ],
)
def test_flux_source_helpers_reject_invalid_shape(helper: object, spec: object) -> None:
    with pytest.raises(ValueError):
        helper(spec)  # type: ignore[operator]


def test_source_control_credential_inputs_match_provider_shape() -> None:
    assert source_control_credential_inputs(ssh_key_data="PRIVATE KEY") == {
        "ssh_key_data": "PRIVATE KEY",
    }


def test_management_kubernetes_credential_inputs_match_provider_shape() -> None:
    assert management_kubernetes_credential_inputs(
        host="https://kubernetes.default.svc",
        bearer_token="TOKEN",
        ssl_ca_cert="CA",
    ) == {
        "bearer_token": "TOKEN",
        "host": "https://kubernetes.default.svc",
        "ssl_ca_cert": "CA",
        "verify_ssl": True,
    }


def test_injectable_kubernetes_credential_type_inputs_are_stable_json() -> None:
    assert injectable_kubernetes_credential_type_inputs() == (
        '{"fields": [{"id": "host", "label": "Kubernetes API endpoint", '
        '"type": "string"}, {"id": "bearer_token", "label": '
        '"Kubernetes API bearer token", "secret": true, "type": "string"}, '
        '{"default": true, "id": "verify_ssl", "label": "Verify SSL", '
        '"type": "boolean"}, {"id": "ssl_ca_cert", "label": '
        '"Kubernetes API certificate authority", "multiline": true, '
        '"secret": true, "type": "string"}], "required": '
        '["host", "bearer_token"]}'
    )


def test_injectable_kubernetes_credential_type_injectors_are_stable_json() -> None:
    assert injectable_kubernetes_credential_type_injectors() == (
        '{"env": {"CA4S_K8S_BEARER_TOKEN": "{{ bearer_token }}", '
        '"CA4S_K8S_HOST": "{{ host }}", "CA4S_K8S_SSL_CA_CERT": '
        '"{{ ssl_ca_cert }}", "CA4S_K8S_VERIFY_SSL": "{{ verify_ssl }}"}}'
    )


def test_dynamic_inventory_variables_are_stable_json() -> None:
    assert dynamic_inventory_variables() == (
        '{"capi_namespace": "default", "compute_node_type": "compute", '
        '"controller_node_type": "controller", '
        '"node_type_label": "slinky.slurm.net/node-type"}'
    )


@pytest.mark.parametrize(
    ("status", "sha"),
    [
        ({"artifact": {"revision": "main@sha1:abc123"}}, "abc123"),
        ({"artifact": {"revision": "abc123"}}, "abc123"),
    ],
)
def test_flux_artifact_revision_sha_extracts_git_sha(status: object, sha: str) -> None:
    assert flux_artifact_revision_sha(status) == sha


def test_flux_artifact_revision_sha_rejects_missing_revision() -> None:
    with pytest.raises(ValueError, match="status.artifact.revision"):
        flux_artifact_revision_sha({})
