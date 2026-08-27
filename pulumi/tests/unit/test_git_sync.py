"""Unit tests for :mod:`gitrepo.git_sync`."""

from __future__ import annotations

import subprocess

import pytest

import gitrepo.git_sync as git_sync
from gitrepo.git_sync import _GitSyncProvider, _split_ssh_url


def _props(**overrides: object) -> dict[str, object]:
    props: dict[str, object] = {
        "repo_url": "ssh://git@gitea-ssh.gitea.svc.cluster.local:22/caps-admin/cluster-api-for-slinky.git",
        "repo_branch": "main",
        "ssh_private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n",
        "ssh_host_public_key": "ssh-ed25519 AAAAFake",
        "ssh_host": "172.18.0.5",
        "ssh_port": 22,
        "source_dir": ".",
        "head_sha": "local-sha",
        "triggers": {},
    }
    props.update(overrides)
    return props


def test_diff_replaces_when_triggers_change() -> None:
    provider = _GitSyncProvider()

    diff = provider.diff(
        "ssh://git@example.invalid/repo.git@local-sha",
        _props(triggers={"generation": "one"}),
        _props(triggers={"generation": "two"}),
    )

    assert diff.changes is True
    assert diff.replaces == ["triggers"]


def test_build_git_url_uses_external_host_and_canonical_path() -> None:
    provider = _GitSyncProvider()

    assert provider._build_git_url(_props()) == (
        "ssh://git@172.18.0.5:22/caps-admin/cluster-api-for-slinky.git"
    )


def test_known_hosts_uses_canonical_flux_host() -> None:
    provider = _GitSyncProvider()

    assert provider._known_hosts(_props()) == (
        "gitea-ssh.gitea.svc.cluster.local ssh-ed25519 AAAAFake\n"
    )


def test_split_ssh_url_rejects_non_ssh() -> None:
    with pytest.raises(ValueError, match="supports only ssh://"):
        _split_ssh_url("https://example.invalid/repo.git")


def test_push_retries_transient_ssh_error(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _GitSyncProvider()
    attempts = 0

    def fake_push_once(props: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.CalledProcessError(
                128,
                ["git", "push"],
                stderr="kex_exchange_identification: read: Connection reset by peer",
            )

    monkeypatch.setattr(provider, "_push_once", fake_push_once)
    monkeypatch.setattr(git_sync.time, "sleep", lambda _seconds: None)

    provider._push(_props())

    assert attempts == 2


def test_push_does_not_retry_non_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _GitSyncProvider()
    attempts = 0

    def fake_push_once(props: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        raise subprocess.CalledProcessError(
            1,
            ["git", "push"],
            stderr="! [rejected] HEAD -> main (non-fast-forward)",
        )

    monkeypatch.setattr(provider, "_push_once", fake_push_once)

    with pytest.raises(subprocess.CalledProcessError):
        provider._push(_props())

    assert attempts == 1