# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Unit tests for :mod:`ctlptl.ctlptl_registry`.

The provider wraps the ``ctlptl`` CLI plus a couple of ``docker`` shell-outs.
All lifecycle methods are pure: they take dicts, shell out, and return dicts.
That means tests in this file should be stdlib-only mocks of
``subprocess.run`` -- no Docker, no kind, no ``ctlptl`` binary required.
"""

from __future__ import annotations

import json
import subprocess

import pytest

# Importing here catches rename regressions at pytest collection time.
from ctlptl.ctlptl_registry import CtlptlRegistry  # noqa: F401
from ctlptl import ctlptl_registry


_DEFAULT_ENV = ["REGISTRY_PROXY_REMOTEURL=https://mirror.gcr.io"]


def _completed(cmd: list[str], stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")


def _registry_list(*registries: tuple[str, int]) -> str:
    return json.dumps(
        {
            "kind": "RegistryList",
            "apiVersion": "ctlptl.dev/v1alpha1",
            "items": [
                {
                    "name": name,
                    "port": port,
                    "status": {
                        "hostPort": port,
                        "env": [
                            "REGISTRY_STORAGE_DELETE_ENABLED=true",
                            *_DEFAULT_ENV,
                        ],
                    },
                }
                for name, port in registries
            ],
        }
    )


def _registry_list_with_env(*registries: tuple[str, int, list[str]]) -> str:
    return json.dumps(
        {
            "kind": "RegistryList",
            "apiVersion": "ctlptl.dev/v1alpha1",
            "items": [
                {
                    "name": name,
                    "port": port,
                    "status": {"hostPort": port, "env": env},
                }
                for name, port, env in registries
            ],
        }
    )


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> ctlptl_registry._CtlptlRegistryProvider:
    monkeypatch.setattr(ctlptl_registry.shutil, "which", lambda name: f"/bin/{name}")
    return ctlptl_registry._CtlptlRegistryProvider()


def test_create_adopts_first_registry_with_matching_prefix(
    provider: ctlptl_registry._CtlptlRegistryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str],
        *,
        input: str | None = None,
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        calls.append(cmd)
        assert input is None
        return _completed(
            cmd,
            _registry_list(
                ("other-registry", 5001),
                ("ca4s-registry-a", 5002),
                ("ca4s-registry-b", 5003),
            ),
        )

    monkeypatch.setattr(ctlptl_registry.subprocess, "run", fake_run)

    result = provider.create({"registry_name": "ca4s-registry", "port": None})

    assert result.id == "ca4s-registry-a"
    assert result.outs is not None
    outs = result.outs
    assert outs["registry_name"] == "ca4s-registry-a"
    assert outs["port"] == 5002
    assert outs["env"] == _DEFAULT_ENV
    assert outs["adopt_existing"] is True
    assert outs["adopted"] is True
    assert outs["delete_on_destroy"] is False
    assert calls == [["ctlptl", "get", "registry", "-o", "json"]]


def test_create_skips_incompatible_adopted_registry(
    provider: ctlptl_registry._CtlptlRegistryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(
        cmd: list[str],
        *,
        input: str | None = None,
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        calls.append((cmd, input))
        if cmd == ["ctlptl", "get", "registry", "-o", "json"]:
            return _completed(
                cmd,
                _registry_list_with_env(
                    ("ca4s-registry-old", 5001, ["REGISTRY_STORAGE_DELETE_ENABLED=true"]),
                ),
            )
        if cmd == ["ctlptl", "apply", "-f", "-"]:
            assert input is not None
            assert "name: ca4s-registry" in input
            assert '- "REGISTRY_PROXY_REMOTEURL=https://mirror.gcr.io"' in input
            return _completed(cmd)
        if cmd == ["ctlptl", "get", "registry", "ca4s-registry", "-o", "json"]:
            return _completed(cmd, json.dumps({"name": "ca4s-registry", "port": 5002}))
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(ctlptl_registry.subprocess, "run", fake_run)

    result = provider.create({"registry_name": "ca4s-registry", "port": 5002})

    assert result.id == "ca4s-registry"
    assert result.outs is not None
    assert result.outs["adopted"] is False
    assert [cmd for cmd, _ in calls] == [
        ["ctlptl", "get", "registry", "-o", "json"],
        ["ctlptl", "apply", "-f", "-"],
        ["ctlptl", "get", "registry", "ca4s-registry", "-o", "json"],
    ]


def test_create_falls_back_to_apply_when_no_adopt_match(
    provider: ctlptl_registry._CtlptlRegistryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(
        cmd: list[str],
        *,
        input: str | None = None,
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        calls.append((cmd, input))
        if cmd == ["ctlptl", "get", "registry", "-o", "json"]:
            return _completed(cmd, _registry_list(("other", 5001)))
        if cmd == ["ctlptl", "apply", "-f", "-"]:
            assert input is not None
            assert "name: wanted" in input
            assert 'listenAddress: "0.0.0.0"' in input
            assert "port: 5050" in input
            assert '- "REGISTRY_PROXY_REMOTEURL=https://mirror.gcr.io"' in input
            return _completed(cmd)
        if cmd == ["ctlptl", "get", "registry", "wanted", "-o", "json"]:
            return _completed(cmd, json.dumps({"name": "wanted", "port": 5050}))
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(ctlptl_registry.subprocess, "run", fake_run)

    result = provider.create({"registry_name": "wanted", "port": 5050})

    assert result.id == "wanted"
    assert result.outs is not None
    outs = result.outs
    assert outs["registry_name"] == "wanted"
    assert outs["port"] == 5050
    assert outs["env"] == _DEFAULT_ENV
    assert outs["adopt_existing"] is True
    assert outs["adopted"] is False
    assert outs["delete_on_destroy"] is False
    assert [cmd for cmd, _ in calls] == [
        ["ctlptl", "get", "registry", "-o", "json"],
        ["ctlptl", "apply", "-f", "-"],
        ["ctlptl", "get", "registry", "wanted", "-o", "json"],
    ]


def test_create_skips_adopted_registry_with_mismatched_pinned_port(
    provider: ctlptl_registry._CtlptlRegistryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(
        cmd: list[str],
        *,
        input: str | None = None,
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        calls.append((cmd, input))
        if cmd == ["ctlptl", "get", "registry", "-o", "json"]:
            return _completed(cmd, _registry_list(("registry-image", 46469)))
        if cmd == ["ctlptl", "apply", "-f", "-"]:
            assert input is not None
            assert "name: registry-image" in input
            assert "port: 5000" in input
            return _completed(cmd)
        if cmd == ["ctlptl", "get", "registry", "registry-image", "-o", "json"]:
            return _completed(cmd, json.dumps({"name": "registry-image", "port": 5000}))
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(ctlptl_registry.subprocess, "run", fake_run)

    result = provider.create({"registry_name": "registry-image", "port": 5000})

    assert result.id == "registry-image"
    assert result.outs is not None
    assert result.outs["port"] == 5000
    assert result.outs["adopted"] is False


@pytest.mark.parametrize("delete_on_destroy", [False, True])
def test_delete_adopted_registry_never_deletes_and_prints_cleanup_hint(
    provider: ctlptl_registry._CtlptlRegistryProvider,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    delete_on_destroy: bool,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return _completed(cmd)

    monkeypatch.setattr(ctlptl_registry.subprocess, "run", fake_run)

    provider.delete(
        "adopted-registry",
        {"adopted": True, "delete_on_destroy": delete_on_destroy},
    )

    assert calls == []
    err = capsys.readouterr().err
    assert "Leaving ctlptl registry 'adopted-registry' in place" in err
    assert "ctlptl delete registry adopted-registry" in err