# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""AWX API readiness fence importable from the init project root."""

from __future__ import annotations

import base64
from collections.abc import Mapping
import time
from typing import Any

import pulumi
from pulumi import ResourceOptions
from pulumi.dynamic import CreateResult, DiffResult, Resource, ResourceProvider, UpdateResult
import requests


AWX_API_SCHEME = "http"
AWX_ADMIN_PASSWORD_SECRET_KEY = "password"
_AWX_API_READY_TIMEOUT_SECONDS = 300
_AWX_API_READY_POLL_INTERVAL_SECONDS = 5
_AWX_DISPATCH_NODE_TYPES = {"control", "hybrid"}
_AWX_READY_NODE_STATE = "ready"


def awx_api_url(*, namespace: str, service_name: str) -> str:
    return f"{AWX_API_SCHEME}://{service_name}.{namespace}.svc.cluster.local"


def decode_secret_data_value(data: Mapping[str, str] | None, key: str) -> str:
    if data is None:
        raise KeyError(f"Secret data[{key!r}] is missing")
    encoded_value = data.get(key)
    if not encoded_value:
        raise KeyError(f"Secret data[{key!r}] is missing")
    return base64.b64decode(encoded_value).decode("utf-8")


def _positive_capacity(value: object) -> bool:
    if not isinstance(value, int | float | str):
        return False
    try:
        return float(value) > 0
    except ValueError:
        return False


def awx_ready_instance_count(payload: object) -> int:
    if not isinstance(payload, Mapping):
        return 0
    results = payload.get("results")
    if not isinstance(results, list):
        return 0

    ready_count = 0
    for instance in results:
        if not isinstance(instance, Mapping):
            continue
        node_type = str(instance.get("node_type") or "")
        if node_type and node_type not in _AWX_DISPATCH_NODE_TYPES:
            continue
        if not instance.get("enabled"):
            continue
        if instance.get("node_state") != _AWX_READY_NODE_STATE:
            continue
        if not _positive_capacity(instance.get("capacity")):
            continue
        ready_count += 1
    return ready_count


class _AWXAPIReadinessProvider(ResourceProvider):
    def _session(self, props: dict[str, Any]) -> requests.Session:
        session = requests.Session()
        session.auth = (str(props["username"]), str(props["password"]))
        return session

    def _url(self, props: dict[str, Any], path: str) -> str:
        return f"{str(props['api_url']).rstrip('/')}{path}"

    def _wait(self, props: dict[str, Any]) -> dict[str, Any]:
        timeout_seconds = int(
            props.get("timeout_seconds") or _AWX_API_READY_TIMEOUT_SECONDS
        )
        deadline = time.monotonic() + timeout_seconds
        session = self._session(props)
        last_error = "AWX API has not been checked yet"

        while True:
            try:
                ping_response = session.get(self._url(props, "/api/v2/ping/"), timeout=30)
                ping_response.raise_for_status()
                instances_response = session.get(
                    self._url(props, "/api/v2/instances/"), timeout=30
                )
                instances_response.raise_for_status()
                ready_count = awx_ready_instance_count(instances_response.json())
                if ready_count > 0:
                    return {
                        "api_url": props["api_url"],
                        "username": props["username"],
                        "timeout_seconds": timeout_seconds,
                        "ready_instance_count": ready_count,
                    }
                last_error = "no enabled ready AWX task/control instances with capacity"
            except requests.RequestException as exc:
                last_error = str(exc)

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "timed out waiting for AWX API dispatch readiness: " + last_error
                )
            time.sleep(_AWX_API_READY_POLL_INTERVAL_SECONDS)

    def create(self, props: dict[str, Any]) -> CreateResult:
        outs = self._wait(props)
        return CreateResult(id_=f"{props['api_url']}/api-ready", outs=outs)

    def diff(self, _id: str, old: dict[str, Any], new: dict[str, Any]) -> DiffResult:
        keys = ("api_url", "username", "password", "timeout_seconds")
        return DiffResult(changes=any(old.get(key) != new.get(key) for key in keys))

    def update(self, _id: str, _old: dict[str, Any], new: dict[str, Any]) -> UpdateResult:
        return UpdateResult(outs=self._wait(new))


class AWXAPIReadiness(Resource):
    ready_instance_count: pulumi.Output[int]

    def __init__(
        self,
        name: str,
        *,
        api_url: pulumi.Input[str],
        username: pulumi.Input[str],
        password: pulumi.Input[str],
        timeout_seconds: pulumi.Input[int] = _AWX_API_READY_TIMEOUT_SECONDS,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            _AWXAPIReadinessProvider(),
            name,
            {
                "api_url": api_url,
                "username": username,
                "password": password,
                "timeout_seconds": timeout_seconds,
            },
            opts,
        )


__all__ = [
    "AWX_ADMIN_PASSWORD_SECRET_KEY",
    "AWXAPIReadiness",
    "_AWXAPIReadinessProvider",
    "awx_api_url",
    "awx_ready_instance_count",
    "decode_secret_data_value",
]