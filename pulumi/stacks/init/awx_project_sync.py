"""AWX project synchronization fence importable from the init project root."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

import pulumi
from pulumi.dynamic import (
    CreateResult,
    DiffResult,
    Resource,
    ResourceProvider,
    UpdateResult,
)
import requests


_SUCCESS_STATUSES = {"successful", "ok"}
_FAILURE_STATUSES = {"failed", "error", "canceled"}
_DEFAULT_TIMEOUT_SECONDS = 300
_POLL_INTERVAL_SECONDS = 5


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def flux_artifact_revision_sha(status: object) -> str:
    artifact = _field(status, "artifact")
    revision = _field(artifact, "revision")
    if not isinstance(revision, str) or not revision:
        raise ValueError("Flux GitRepository status.artifact.revision is missing")
    if "@sha" in revision and ":" in revision:
        return revision.rsplit(":", 1)[-1]
    return revision


class _AWXProjectSyncProvider(ResourceProvider):
    def _session(self, props: dict[str, Any]) -> requests.Session:
        session = requests.Session()
        session.auth = (str(props["username"]), str(props["password"]))
        return session

    def _url(self, props: dict[str, Any], path: str) -> str:
        return f"{str(props['api_url']).rstrip('/')}{path}"

    def _project(self, session: requests.Session, props: dict[str, Any]) -> dict[str, Any]:
        response = session.get(
            self._url(props, f"/api/v2/projects/{int(props['project_id'])}/"),
            timeout=30,
        )
        response.raise_for_status()
        project = response.json()
        if not isinstance(project, dict):
            raise RuntimeError("AWX project response must be an object")
        return project

    def _trigger_update(self, session: requests.Session, props: dict[str, Any]) -> None:
        response = session.post(
            self._url(props, f"/api/v2/projects/{int(props['project_id'])}/update/"),
            json={},
            timeout=30,
        )
        if response.status_code not in {200, 201, 202, 204}:
            raise RuntimeError(
                "failed to start AWX project update: "
                f"HTTP {response.status_code}: {response.text[:500]}"
            )

    def _sync(self, props: dict[str, Any]) -> dict[str, Any]:
        expected_revision = str(props["expected_revision"])
        timeout_seconds = int(props.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)
        deadline = time.monotonic() + timeout_seconds
        session = self._session(props)

        project = self._project(session, props)
        if project.get("scm_revision") != expected_revision:
            self._trigger_update(session, props)

        while True:
            project = self._project(session, props)
            status = str(project.get("status") or "")
            if (
                project.get("scm_revision") == expected_revision
                and status in _SUCCESS_STATUSES
            ):
                return {
                    "api_url": props["api_url"],
                    "username": props["username"],
                    "project_id": props["project_id"],
                    "expected_revision": expected_revision,
                    "timeout_seconds": timeout_seconds,
                    "project_status": status,
                    "synced_revision": project.get("scm_revision"),
                }
            if status in _FAILURE_STATUSES:
                raise RuntimeError(
                    "AWX project update reached terminal status "
                    f"{status!r} before revision {expected_revision!r} was synced"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "timed out waiting for AWX project "
                    f"{props['project_id']} to sync revision {expected_revision}"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)

    def create(self, props: dict[str, Any]) -> CreateResult:
        outs = self._sync(props)
        return CreateResult(
            id_=f"{props['project_id']}@{outs['synced_revision']}",
            outs=outs,
        )

    def diff(self, _id: str, old: dict[str, Any], new: dict[str, Any]) -> DiffResult:
        keys = (
            "api_url",
            "username",
            "project_id",
            "expected_revision",
            "timeout_seconds",
        )
        return DiffResult(changes=any(old.get(key) != new.get(key) for key in keys))

    def update(self, _id: str, _old: dict[str, Any], new: dict[str, Any]) -> UpdateResult:
        return UpdateResult(outs=self._sync(new))


class AWXProjectSync(Resource):
    synced_revision: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        api_url: pulumi.Input[str],
        username: pulumi.Input[str],
        password: pulumi.Input[str],
        project_id: pulumi.Input[float],
        expected_revision: pulumi.Input[str],
        timeout_seconds: pulumi.Input[int] = _DEFAULT_TIMEOUT_SECONDS,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            _AWXProjectSyncProvider(),
            name,
            {
                "api_url": api_url,
                "username": username,
                "password": password,
                "project_id": project_id,
                "expected_revision": expected_revision,
                "timeout_seconds": timeout_seconds,
            },
            opts,
        )


__all__ = [
    "AWXProjectSync",
    "_AWXProjectSyncProvider",
    "flux_artifact_revision_sha",
]