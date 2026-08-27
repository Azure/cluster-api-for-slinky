# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Shared helpers for source-ref OCI objects in a local ctlptl registry."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from typing import Iterator

_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    )
)


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(
            f"required binary '{name}' not found in PATH; install it before running pulumi"
        )
    return path


def run(
    cmd: list[str],
    *,
    cwd: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command {cmd!r} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def required_str(props: dict, name: str) -> str:
    value = props.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} must be a non-empty string")
    return value


def required_int(props: dict, name: str) -> int:
    value = props.get(name)
    if isinstance(value, bool):
        raise RuntimeError(f"{name} must be a positive integer")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def resolve_source_commit(source_path: str, source_ref: str) -> str:
    require_binary("git")
    result = run(["git", "-C", source_path, "rev-parse", f"{source_ref}^{{commit}}"])
    commit = result.stdout.strip()
    if not commit:
        raise RuntimeError(f"source_ref {source_ref!r} did not resolve to a commit")
    return commit


@contextmanager
def remote_source_repository(
    repository_url: str,
    source_ref: str,
) -> Iterator[tuple[str, str]]:
    require_binary("git")
    repository_path = tempfile.mkdtemp(prefix="ca4s-source-")
    try:
        run(["git", "init", "--bare", repository_path])
        run(
            [
                "git",
                "-C",
                repository_path,
                "fetch",
                "--depth=1",
                repository_url,
                source_ref,
            ]
        )
        result = run(
            ["git", "-C", repository_path, "rev-parse", "FETCH_HEAD^{commit}"]
        )
        source_commit = result.stdout.strip()
        if not source_commit:
            raise RuntimeError(f"source_ref {source_ref!r} did not resolve to a commit")
        yield repository_path, source_commit
    finally:
        shutil.rmtree(repository_path, ignore_errors=True)


@contextmanager
def source_repository(
    props: dict,
    *,
    resolve_commit: Callable[[str, str], str] = resolve_source_commit,
) -> Iterator[tuple[str, str, str | None, str | None]]:
    source_path = props.get("source_path")
    repository_url = props.get("repository_url")
    if source_path is not None and repository_url is not None:
        raise RuntimeError("source_path and repository_url are mutually exclusive")
    source_ref = required_str(props, "source_ref")
    if source_path is not None:
        if not isinstance(source_path, str) or not source_path:
            raise RuntimeError("source_path must be a non-empty string")
        yield source_path, resolve_commit(source_path, source_ref), source_path, None
        return
    if not isinstance(repository_url, str) or not repository_url:
        raise RuntimeError("exactly one of source_path or repository_url is required")
    with remote_source_repository(repository_url, source_ref) as (
        repository_path,
        source_commit,
    ):
        yield repository_path, source_commit, None, repository_url


def source_tag(source_commit: str) -> str:
    return f"source-{source_commit[:12]}"


def host_ref(registry_port: int, object_name: str, object_tag: str) -> str:
    return f"localhost:{registry_port}/{object_name}:{object_tag}"


def cluster_ref(registry_name: str, object_name: str, object_tag: str) -> str:
    return f"{registry_name}:5000/{object_name}:{object_tag}"


def manifest_exists(registry_port: int, object_name: str, object_tag: str) -> bool:
    url = f"http://localhost:{registry_port}/v2/{object_name}/manifests/{object_tag}"
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"Accept": _MANIFEST_ACCEPT},
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise RuntimeError(f"registry manifest probe failed for {url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"registry manifest probe failed for {url}: {exc}") from exc


def ensure_source_ref_object(
    props: dict,
    *,
    object_name: str,
    object_name_key: str,
    object_tag_key: str,
    host_ref_key: str,
    cluster_ref_key: str,
    extra_outputs: Mapping[str, object] | None = None,
    build: Callable[[str, str, str], None],
    resolve_commit: Callable[[str, str], str] = resolve_source_commit,
    probe_manifest: Callable[[int, str, str], bool] = manifest_exists,
) -> dict[str, object]:
    source_ref = required_str(props, "source_ref")
    registry_name = required_str(props, "registry_name")
    registry_port = required_int(props, "registry_port")
    with source_repository(props, resolve_commit=resolve_commit) as (
        repository_path,
        source_commit,
        source_path,
        repository_url,
    ):
        object_tag = source_tag(source_commit)
        object_host_ref = host_ref(registry_port, object_name, object_tag)
        object_cluster_ref = cluster_ref(registry_name, object_name, object_tag)

        built = False
        if not probe_manifest(registry_port, object_name, object_tag):
            build(repository_path, source_commit, object_host_ref)
            built = True

    outs = {
        "source_path": source_path,
        "repository_url": repository_url,
        "source_ref": source_ref,
        "source_commit": source_commit,
        "registry_name": registry_name,
        "registry_port": registry_port,
        object_name_key: object_name,
        object_tag_key: object_tag,
        host_ref_key: object_host_ref,
        cluster_ref_key: object_cluster_ref,
        "built": built,
    }
    if extra_outputs is not None:
        outs.update(extra_outputs)
    return outs


def source_commit_for_read(
    props: dict,
    *,
    resolve_commit: Callable[[str, str], str] = resolve_source_commit,
) -> str:
    with source_repository(props, resolve_commit=resolve_commit) as (_, commit, _, _):
        return commit


def has_diff(olds: dict, news: dict, keys: tuple[str, ...]) -> bool:
    return any(olds.get(key) != news.get(key) for key in keys)