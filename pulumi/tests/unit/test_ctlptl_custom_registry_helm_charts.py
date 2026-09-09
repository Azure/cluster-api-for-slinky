# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from ctlptl import ctlptl_custom_registry_helm_charts as helm_charts

_SOURCE_COMMIT = "1234567890abcdef1234567890abcdef12345678"
_CHART_VERSION = "0.0.0-source1234567890ab"


def _props() -> dict[str, object]:
    return {
        "source_path": "/src/slurm-operator",
        "source_ref": "feature/slinky",
        "registry_name": "registry-test",
        "registry_port": 5002,
    }


def test_create_skips_build_when_all_charts_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        helm_charts,
        "_resolve_source_commit",
        lambda source_path, source_ref: _SOURCE_COMMIT,
    )
    monkeypatch.setattr(helm_charts, "_charts_exist", lambda *args: True)
    monkeypatch.setattr(
        helm_charts,
        "_build_and_push_charts",
        lambda **kwargs: pytest.fail("existing charts must not be rebuilt"),
    )

    result = helm_charts._CtlptlCustomRegistryHelmChartsProvider().create(_props())

    assert result.id == f"registry-test:5000/charts/slinky:{_CHART_VERSION}"
    assert result.outs is not None
    assert result.outs["built"] is False
    assert result.outs["chart_version"] == _CHART_VERSION


def test_create_builds_when_any_chart_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds: list[dict[str, object]] = []
    monkeypatch.setattr(
        helm_charts,
        "_resolve_source_commit",
        lambda source_path, source_ref: _SOURCE_COMMIT,
    )
    monkeypatch.setattr(helm_charts, "_charts_exist", lambda *args: False)
    monkeypatch.setattr(
        helm_charts,
        "_build_and_push_charts",
        lambda **kwargs: builds.append(kwargs),
    )

    result = helm_charts._CtlptlCustomRegistryHelmChartsProvider().create(_props())

    assert result.outs is not None
    assert result.outs["built"] is True
    assert builds == [
        {
            "source_path": "/src/slurm-operator",
            "source_commit": _SOURCE_COMMIT,
            "registry_port": 5002,
            "chart_version": _CHART_VERSION,
        }
    ]


def test_build_packages_and_pushes_all_charts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str | None, bool]] = []

    @contextmanager
    def fake_worktree(
        repository_path: str,
        source_commit: str,
        *,
        prefix: str,
    ) -> Iterator[str]:
        assert repository_path == "/src/slurm-operator"
        assert source_commit == _SOURCE_COMMIT
        assert prefix == "ca4s-slinky-charts-"
        yield "/tmp/slinky-worktree"

    monkeypatch.setattr(
        helm_charts.oci_object,
        "require_binary",
        lambda name: "/bin/helm" if name == "helm" else f"/bin/{name}",
    )
    monkeypatch.setattr(
        helm_charts.oci_object,
        "run",
        lambda cmd, cwd=None, check=True: calls.append((cmd, cwd, check)),
    )
    monkeypatch.setattr(
        helm_charts.oci_object,
        "detached_worktree",
        fake_worktree,
    )
    monkeypatch.setattr(helm_charts.Path, "mkdir", lambda self: None)

    helm_charts._build_and_push_charts(
        source_path="/src/slurm-operator",
        source_commit=_SOURCE_COMMIT,
        registry_port=5002,
        chart_version=_CHART_VERSION,
    )

    assert calls[0] == (
        ["make", f"VERSION={_CHART_VERSION}", "version-match"],
        "/tmp/slinky-worktree",
        True,
    )
    package_calls = [call for call in calls if "package" in call[0]]
    push_calls = [call for call in calls if "push" in call[0]]
    assert len(package_calls) == 3
    assert len(push_calls) == 3
    assert all("--plain-http" in call[0] for call in push_calls)
