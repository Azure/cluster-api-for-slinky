from __future__ import annotations

import base64

import pytest

from stacks.control_plane.awx import awx_api_url, decode_secret_data_value
from stacks.control_plane.awx._configuration import (
    flux_source_branch,
    flux_source_secret_name,
    flux_source_url,
    project_name_from_scm_url,
    management_kubernetes_credential_inputs,
    source_control_credential_inputs,
)


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


def test_source_control_credential_inputs_are_stable_json() -> None:
    assert (
        source_control_credential_inputs(ssh_key_data="PRIVATE KEY")
        == '{"ssh_key_data": "PRIVATE KEY"}'
    )


def test_management_kubernetes_credential_inputs_are_stable_json() -> None:
    assert management_kubernetes_credential_inputs(
        host="https://kubernetes.default.svc",
        bearer_token="TOKEN",
        ssl_ca_cert="CA",
    ) == (
        '{"bearer_token": "TOKEN", "host": "https://kubernetes.default.svc", '
        '"ssl_ca_cert": "CA", "verify_ssl": true}'
    )