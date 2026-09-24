"""Git source resolution and temporary build worktrees."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"required binary '{name}' not found in PATH; install it before running pulumi")
    return path


def run(
    cmd: list[str], *, cwd: str | None = None, stdin: str | None = None, check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command {cmd!r} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def required_str(props: dict, name: str) -> str:
    value = props.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} must be a non-empty string")
    return value


def resolve_source_commit(source_path: str, source_ref: str) -> str:
    require_binary("git")
    result = run(["git", "-C", source_path, "rev-parse", f"{source_ref}^{{commit}}"])
    commit = result.stdout.strip()
    if not commit:
        raise RuntimeError(f"source_ref {source_ref!r} did not resolve to a commit")
    return commit


@contextmanager
def detached_worktree(repository_path: str, source_commit: str, *, prefix: str) -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix=prefix)
    try:
        run(["git", "-C", repository_path, "worktree", "add", "--detach", directory, source_commit])
        yield directory
    finally:
        run(["git", "-C", repository_path, "worktree", "remove", "--force", directory], check=False)
        shutil.rmtree(directory, ignore_errors=True)


@contextmanager
def remote_source_repository(repository_url: str, source_ref: str) -> Iterator[tuple[str, str]]:
    require_binary("git")
    repository_path = tempfile.mkdtemp(prefix="ca4s-source-")
    try:
        run(["git", "init", "--bare", repository_path])
        run(["git", "-C", repository_path, "fetch", "--depth=1", repository_url, source_ref])
        result = run(["git", "-C", repository_path, "rev-parse", "FETCH_HEAD^{commit}"])
        source_commit = result.stdout.strip()
        if not source_commit:
            raise RuntimeError(f"source_ref {source_ref!r} did not resolve to a commit")
        yield repository_path, source_commit
    finally:
        shutil.rmtree(repository_path, ignore_errors=True)


@contextmanager
def source_repository(props: dict) -> Iterator[tuple[str, str]]:
    source_path = props.get("source_path")
    repository_url = props.get("repository_url")
    if source_path is not None and repository_url is not None:
        raise RuntimeError("source_path and repository_url are mutually exclusive")
    source_ref = required_str(props, "source_ref")
    if source_path is not None:
        source_path = required_str(props, "source_path")
        yield source_path, resolve_source_commit(source_path, source_ref)
        return
    repository_url = required_str(props, "repository_url")
    with remote_source_repository(repository_url, source_ref) as resolved:
        yield resolved


def source_tag(source_commit: str) -> str:
    return f"source-{source_commit[:12]}"


def source_commit_for_read(props: dict) -> str:
    with source_repository(props) as (_, commit):
        return commit


def has_diff(olds: dict, news: dict, keys: tuple[str, ...]) -> bool:
    return any(olds.get(key) != news.get(key) for key in keys)