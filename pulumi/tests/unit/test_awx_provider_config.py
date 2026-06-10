from __future__ import annotations

import base64

import pytest

from stacks.control_plane.awx import awx_api_url, decode_secret_data_value


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