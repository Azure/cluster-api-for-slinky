"""Unit tests for outer stack config routing."""

from __future__ import annotations

from typing import Any

from stack import _azure_workload_requested


class _Config:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str) -> Any:
        value = self._values.get(key)
        return value if isinstance(value, str) else None

    def get_object(self, key: str) -> object | None:
        value = self._values.get(key)
        return value if not isinstance(value, bool | str) else None

    def get_bool(self, key: str) -> bool | None:
        value = self._values.get(key)
        return value if isinstance(value, bool) else None


def test_skip_in_cluster_preflight_alone_does_not_request_azure() -> None:
    assert not _azure_workload_requested(
        _Config({"skip_in_cluster_preflight": True})
    )


def test_azure_identity_hint_requests_azure() -> None:
    assert _azure_workload_requested(_Config({"azureClientId": "client-id"}))


def test_azure_object_config_requests_azure() -> None:
    assert _azure_workload_requested(
        _Config({"azureAdditionalTags": {"owner": "platform"}})
    )