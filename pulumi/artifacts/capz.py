"""Build and publish the CAPZ provider bundle."""

from __future__ import annotations

from pathlib import Path

from artifacts import source
from artifacts.destination import RegistrySession


_ARTIFACT_NAME = "capz/cluster-api-provider-azure"
_ARTIFACT_FILES = ["metadata.yaml", "infrastructure-components.yaml"]


def artifact_tags(options: dict, commit: str) -> dict[str, str]:
    return {_ARTIFACT_NAME: source.source_tag(commit)}


def build_and_publish(worktree: str, options: dict, tags: dict[str, str], session: RegistrySession) -> None:
    directory = _build_bundle(worktree)
    session.push_files(_ARTIFACT_NAME, tags[_ARTIFACT_NAME], str(directory), _ARTIFACT_FILES)


def _build_bundle(worktree: str) -> Path:
    source.require_binary("make")
    source.run(["make", "release-manifests", "release-metadata"], cwd=worktree)
    directory = Path(worktree) / "out"
    missing = [name for name in _ARTIFACT_FILES if not (directory / name).is_file()]
    if missing:
        raise RuntimeError(f"CAPZ release artifact generation did not produce {missing!r}")
    return directory

