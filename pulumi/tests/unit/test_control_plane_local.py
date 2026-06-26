from __future__ import annotations

from typing import Any

import pulumi
import pytest

from stacks.control_plane import control_plane_kind
from stacks.control_plane import control_plane_config
from stacks.control_plane.control_plane_config import ControlPlaneKindConfig
from stacks.control_plane.control_plane_kind import (
    ControlPlaneKind,
    ControlPlaneKindSpec,
    ManagementAWXControlPlaneOutputs,
)


def test_parse_control_plane_kind_config_defaults_awx_enabled() -> None:
    assert control_plane_config.parse_control_plane_kind_config(None) == (
        ControlPlaneKindConfig(enable_awx=True)
    )


def test_parse_control_plane_kind_config_reads_awx_enabled() -> None:
    assert control_plane_config.parse_control_plane_kind_config(
        {"awx": {"enabled": False}}
    ) == ControlPlaneKindConfig(enable_awx=False)


def test_parse_control_plane_kind_config_rejects_non_bool_awx_enabled() -> None:
    with pytest.raises(ValueError, match="controlPlane.awx.enabled must be a boolean"):
        control_plane_config.parse_control_plane_kind_config(
            {"awx": {"enabled": "false"}}
        )


class _FakeCertManager:
    namespace = "cert-manager"

    def __init__(self, name: str, *, opts: pulumi.ResourceOptions | None = None) -> None:
        self.name = name
        self.opts = opts


class _FakeClusterAPIOperator:
    namespace = "capi-system"
    provider_version = "v1.13.2"
    provider_namespaces = {"docker": "capd-system"}

    def __init__(
        self,
        name: str,
        *,
        cert_manager: _FakeCertManager,
        infrastructure_providers: tuple[str, ...] = ("docker",),
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        self.name = name
        self.cert_manager = cert_manager
        self.infrastructure_providers = infrastructure_providers
        self.opts = opts


class _FakeManagementAWXControlPlane:
    calls: list[dict[str, object]] = []

    def __init__(self, name: str, **kwargs: object) -> None:
        self.calls.append({"name": name, **kwargs})
        self.outputs = ManagementAWXControlPlaneOutputs(
            operator_namespace=pulumi.Output.from_input("awx"),
            instance_name=pulumi.Output.from_input("awx"),
            service_name=pulumi.Output.from_input("awx-service"),
            api_url=pulumi.Output.from_input(
                "http://awx-service.awx.svc.cluster.local"
            ),
            admin_user=pulumi.Output.from_input("admin"),
            admin_password=pulumi.Output.from_input("password"),
            admin_password_secret=pulumi.Output.from_input("awx-admin-password"),
            organization_id=pulumi.Output.from_input(1.0),
            project_id=pulumi.Output.from_input(2.0),
            project_name=pulumi.Output.from_input("gitops"),
            scm_credential_id=pulumi.Output.from_input(3.0),
            management_kubernetes_credential_id=pulumi.Output.from_input(4.0),
            dynamic_inventory_id=pulumi.Output.from_input(5.0),
            dynamic_inventory_source_id=pulumi.Output.from_input(6.0),
            cluster_state_job_template_id=pulumi.Output.from_input(7.0),
            ready=pulumi.Output.from_input(True),
        )


def _patch_pulumi_component(monkeypatch: Any) -> None:
    def init(
        self: pulumi.ComponentResource,
        t: str,
        name: str,
        props: dict[str, object] | None = None,
        opts: pulumi.ResourceOptions | None = None,
        remote: bool = False,
        package_ref: object | None = None,
    ) -> None:
        self._test_type = t
        self._test_name = name

    def register_outputs(
        self: pulumi.ComponentResource,
        outputs: dict[str, object],
    ) -> None:
        self._test_outputs = outputs

    monkeypatch.setattr(pulumi.ComponentResource, "__init__", init)
    monkeypatch.setattr(pulumi.ComponentResource, "register_outputs", register_outputs)


def _patch_local_children(monkeypatch: Any) -> None:
    _FakeManagementAWXControlPlane.calls = []
    monkeypatch.setattr(control_plane_kind, "CertManager", _FakeCertManager)
    monkeypatch.setattr(
        control_plane_kind,
        "ClusterAPIOperator",
        _FakeClusterAPIOperator,
    )
    monkeypatch.setattr(
        control_plane_kind,
        "ManagementAWXControlPlane",
        _FakeManagementAWXControlPlane,
    )


def test_control_plane_local_skips_awx_when_disabled(monkeypatch: Any) -> None:
    _patch_pulumi_component(monkeypatch)
    _patch_local_children(monkeypatch)

    control_plane = ControlPlaneKind(
        "control-plane",
        flux_source_namespace="pko-system",
        flux_source_name="gitops-source",
        spec=ControlPlaneKindSpec(
            infrastructure_providers=("docker",),
            enable_awx=False,
        ),
    )

    assert _FakeManagementAWXControlPlane.calls == []
    assert control_plane.awx is None
    assert "awx_enabled" in control_plane._test_outputs
    assert control_plane._test_outputs["awx"] is None


def test_control_plane_local_instantiates_awx_by_default(monkeypatch: Any) -> None:
    _patch_pulumi_component(monkeypatch)
    _patch_local_children(monkeypatch)

    control_plane = ControlPlaneKind(
        "control-plane",
        flux_source_namespace="pko-system",
        flux_source_name="gitops-source",
        spec=ControlPlaneKindSpec(
            infrastructure_providers=("docker",),
            enable_awx=True,
        ),
    )

    assert len(_FakeManagementAWXControlPlane.calls) == 1
    call = _FakeManagementAWXControlPlane.calls[0]
    assert call["name"] == "awx"
    assert call["flux_source_namespace"] == "pko-system"
    assert call["flux_source_name"] == "gitops-source"
    assert control_plane.awx is not None
    assert "project_id" in control_plane._test_outputs["awx"]
    assert "provider" not in control_plane._test_outputs["awx"]