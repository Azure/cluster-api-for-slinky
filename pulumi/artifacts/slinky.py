"""Package Slinky charts and publish them to a registry destination."""

from __future__ import annotations

from pathlib import Path

from artifacts import source
from artifacts.destination import RegistrySession


_CHART_NAMES = ("slurm-operator-crds", "slurm-operator", "slurm")
RESOURCE_ID_REPOSITORY = "charts/slinky"


def artifact_tags(options: dict, commit: str) -> dict[str, str]:
    return {f"charts/{name}": _chart_version(commit) for name in _CHART_NAMES}


def build_and_publish(worktree: str, options: dict, tags: dict[str, str], session: RegistrySession) -> None:
    for archive in _build_charts(worktree, next(iter(tags.values()))):
        session.push_chart(archive, "charts")


def _chart_version(commit: str) -> str:
    return f"0.0.0-source{commit[:12]}"


def _build_charts(worktree: str, version: str) -> list[Path]:
    helm = source.require_binary("helm")
    source.require_binary("make")
    source.run(["make", f"VERSION={version}", "version-match"], cwd=worktree)
    directory = Path(worktree) / "dist"
    directory.mkdir()
    for name in _CHART_NAMES:
        source.run([
            helm, "package", "--dependency-update", "--destination", str(directory), f"helm/{name}",
        ], cwd=worktree)
    return [directory / f"{name}-{version}.tgz" for name in _CHART_NAMES]

