"""Unit tests for :mod:`gitrepo.gitea_sync`."""

from __future__ import annotations

from gitrepo.gitea_sync import _GiteaSyncProvider


def _props(**overrides: object) -> dict[str, object]:
    props: dict[str, object] = {
        "ssh_private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n",
        "ssh_host_public_key": "ssh-ed25519 AAAAFake",
        "ssh_host": "172.18.0.5",
        "ssh_host_alias": "gitea-ssh.gitea.svc.cluster.local",
        "ssh_port": 22,
        "owner": "caps-admin",
        "repo_name": "cluster-api-provider-slinky",
        "default_branch": "main",
        "source_dir": ".",
        "head_sha": "local-sha",
        "triggers": {},
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
