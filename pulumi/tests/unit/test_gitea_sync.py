"""Unit tests for :mod:`gitrepo.gitea_sync`."""

from __future__ import annotations

from gitrepo.gitea_sync import _GiteaSyncProvider


def _props(**overrides: object) -> dict[str, object]:
    props: dict[str, object] = {
        "ssh_key_secret_namespace": "gitea",
        "ssh_key_secret_name": "gitea-ssh-user-key",
        "ssh_private_key_secret_key": "privateKey",
        "ssh_host_key_secret_namespace": "gitea",
        "ssh_host_key_secret_name": "gitea-ssh-host-key",
        "ssh_host_public_key_secret_key": "publicKey",
        "owner": "caps-admin",
        "repo_name": "cluster-api-provider-slinky",
        "default_branch": "main",
        "source_dir": ".",
        "head_sha": "local-sha",
        "triggers": {},
        "kubeconfig": "kubeconfig-bytes",
        "service_namespace": "gitea",
        "service_name": "gitea-ssh",
        "service_port": 22.0,
    }
    props.update(overrides)
    return props


def test_diff_replaces_when_triggers_change() -> None:
    provider = _GiteaSyncProvider()

    diff = provider.diff(
        "caps-admin/repo@local-sha",
        _props(triggers={"generation": "one"}),
        _props(triggers={"generation": "two"}),
    )

    assert diff.changes is True
    assert diff.replaces == ["triggers"]
