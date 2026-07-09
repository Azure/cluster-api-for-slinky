"""Unit tests for :mod:`ctlptl.ctlptl_cluster`.

Mocks ``subprocess.run`` for the ``ctlptl`` and ``kubectl`` shell-outs.
Also mocks the kubeconfig file lookup since the provider reads
``$KUBECONFIG`` / ``~/.kube/config`` directly in ``read()`` and ``create()``
to surface the cluster's kubeconfig as a Pulumi Output.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ctlptl.ctlptl_cluster import CtlptlCluster  # noqa: F401
from ctlptl import ctlptl_cluster


def test_render_generates_docker_hub_hosts_for_registry_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "ctlptl"
    monkeypatch.setattr(ctlptl_cluster, "_GENERATED_CONFIG_DIR", state_dir)
    monkeypatch.delenv("HOME", raising=False)

    rendered = ctlptl_cluster._render("kind-mgmt-test", "registry-test")

    hosts_path = state_dir / "kind-mgmt-test" / "docker.io" / "hosts.toml"
    hosts_toml = hosts_path.read_text(encoding="utf-8")

    assert "registry: registry-test" not in rendered
    assert "${HOME}" not in rendered
    assert "/root/.kube" not in rendered
    assert f"hostPath: {hosts_path}" in rendered
    assert "containerPath: /etc/containerd/certs.d/docker.io/hosts.toml" in rendered
    assert 'server = "https://registry-1.docker.io"' in hosts_toml
    assert '[host."http://registry-test:5000"]' in hosts_toml
    assert 'capabilities = ["pull", "resolve"]' in hosts_toml


def test_render_mounts_custom_http_registries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "ctlptl"
    monkeypatch.setattr(ctlptl_cluster, "_GENERATED_CONFIG_DIR", state_dir)

    rendered = ctlptl_cluster._render(
        "kind-mgmt-test",
        "registry-test",
        ["custom-registry"],
    )

    hosts_path = (
        state_dir
        / "kind-mgmt-test"
        / "custom-registry:5000"
        / "hosts.toml"
    )
    hosts_toml = hosts_path.read_text(encoding="utf-8")

    assert f"hostPath: {hosts_path}" in rendered
    assert (
        "containerPath: "
        "/etc/containerd/certs.d/custom-registry:5000/hosts.toml"
    ) in rendered
    assert 'server = "http://custom-registry:5000"' in hosts_toml
    assert '[host."http://custom-registry:5000"]' in hosts_toml
    assert 'capabilities = ["pull", "resolve"]' in hosts_toml


def test_create_connects_cache_and_custom_registries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "ctlptl"
    connected_registries: list[str] = []
    applied_manifests: list[str] = []

    monkeypatch.setattr(ctlptl_cluster, "_GENERATED_CONFIG_DIR", state_dir)
    monkeypatch.setattr(ctlptl_cluster, "_require_binary", lambda _name: _name)
    monkeypatch.setattr(ctlptl_cluster, "_fetch_kubeconfig", lambda _context: "kubeconfig")
    monkeypatch.setattr(
        ctlptl_cluster,
        "_connect_registry_to_kind_network",
        connected_registries.append,
    )

    def fake_run(
        cmd: list[str],
        *,
        stdin: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        applied_manifests.append(stdin or "")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ctlptl_cluster, "_run", fake_run)

    result = ctlptl_cluster._CtlptlClusterProvider().create(
        {
            "cluster_name": "kind-mgmt-test",
            "registry_name": "registry-test",
            "custom_registry_names": ["custom-registry"],
        }
    )

    assert result.outs["custom_registry_names"] == ["custom-registry"]
    assert connected_registries == ["registry-test", "custom-registry"]
    assert "custom-registry:5000/hosts.toml" in applied_manifests[0]


def test_render_requires_registry_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOME", raising=False)

    with pytest.raises(RuntimeError, match="registry_name is required"):
        ctlptl_cluster._render("kind-mgmt-test", None)


@pytest.mark.skip(reason="TODO: stub subprocess.run for `ctlptl apply`; assert ``${CLUSTER_NAME}`` is substituted with the autonamed cluster name and the registry_name input is rendered into the generated Docker Hub hosts file")
def test_create_substitutes_template_placeholders() -> None:
    pass


@pytest.mark.skip(reason="TODO: assert .create() returns a result dict carrying ``cluster_name`` (autonamed), ``context`` (= ``kind-<cluster_name>``), and ``kubeconfig`` (full YAML bytes, NOT a path)")
def test_create_emits_full_kubeconfig_inline() -> None:
    pass


@pytest.mark.skip(reason="TODO: assert .diff() marks ``cluster_name`` as replace=True (kind doesn't support rename) but treats ``registry_name`` updates as no-op IF the registry hostname didn't change in the new manifest (the registry container is upstream of the cluster)")
def test_diff_distinguishes_replace_vs_noop() -> None:
    pass


@pytest.mark.skip(reason="TODO: simulate ``kind get clusters`` returning the expected name in .read(); assert ``kubeconfig`` is re-harvested live (drift detection) rather than trusted from props")
def test_read_re_harvests_kubeconfig() -> None:
    pass


@pytest.mark.skip(reason="TODO: stub subprocess.run for `ctlptl delete cluster`; assert .delete() is idempotent if the cluster is already gone")
def test_delete_idempotent_on_missing_cluster() -> None:
    pass


@pytest.mark.skip(reason="TODO: round-trip cloudpickle of ctlptl.ctlptl_cluster._CtlptlClusterProvider() and assert it unpickles cleanly; this guards the module-path invariant documented in the source (any rename of ``ctlptl.ctlptl_cluster`` breaks Pulumi state)")
def test_provider_cloudpickle_roundtrip() -> None:
    pass
