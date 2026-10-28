# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Tests for the manifest-driven Pulumi SDK regeneration workflow."""

from __future__ import annotations

import json
from pathlib import Path
import runpy

import pytest
import yaml


_REPO_ROOT = Path(__file__).resolve().parents[3]
_GENERATOR = runpy.run_path(str(_REPO_ROOT / "scripts/regenerate_sdks.py"))
GenerationError = _GENERATOR["GenerationError"]
artifact_paths = _GENERATOR["artifact_paths"]
clean_artifacts = _GENERATOR["clean_artifacts"]
compare_trees = _GENERATOR["compare_trees"]
load_manifest = _GENERATOR["load_manifest"]
normalize_crd_utilities = _GENERATOR["_normalize_crd_utilities"]
normalize_terraform_utilities = _GENERATOR["_normalize_terraform_utilities"]
remove_flux_provider_shim = _GENERATOR["_remove_flux_provider_shim"]
verify_checksum = _GENERATOR["verify_checksum"]


def test_committed_manifest_is_valid() -> None:
    manifest = load_manifest()

    assert set(manifest["sdks"]) == {"awx", "capi-core", "flux", "gitea", "local"}


def test_capi_runtime_and_generated_crd_sdk_are_aligned() -> None:
    from stacks.control_plane.capi._operator import CAPI_PROVIDER_VERSION

    manifest = load_manifest()
    capi = manifest["sdks"]["capi-core"]
    assert capi["version"] == CAPI_PROVIDER_VERSION.removeprefix("v")
    assert f"/{CAPI_PROVIDER_VERSION}/" in capi["sourceUrl"]
    assert capi["kubernetesVersion"] == manifest["sdks"]["flux"]["kubernetesVersion"]


def test_terraform_project_packages_match_generated_sources() -> None:
    manifest = load_manifest()
    project = yaml.safe_load((_REPO_ROOT / "pulumi/Pulumi.yaml").read_text())
    for name, sdk in manifest["sdks"].items():
        if sdk["kind"] == "terraform":
            package = project["packages"][name]
            assert package["version"] == manifest["tools"]["terraformBridge"]["version"]
            assert package["parameters"] == [sdk["provider"], sdk["version"]]


def test_rejects_unknown_manifest_schema(tmp_path: Path) -> None:
    manifest = tmp_path / "sources.json"
    manifest.write_text(
        json.dumps({"schemaVersion": 2, "sdks": {"example": {}}}),
        encoding="utf-8",
    )

    with pytest.raises(GenerationError, match="schemaVersion"):
        load_manifest(manifest)


def test_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.yaml"
    source.write_text("content", encoding="utf-8")

    with pytest.raises(GenerationError, match="checksum mismatch"):
        verify_checksum(source, "0" * 64)


def test_artifact_cleanup_is_checkable(tmp_path: Path) -> None:
    artifacts = [
        tmp_path / "sdk/build/generated.py",
        tmp_path / "sdk/package.egg-info/PKG-INFO",
        tmp_path / "sdk/package/__pycache__/module.pyc",
    ]
    for artifact in artifacts:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("artifact", encoding="utf-8")

    assert len(artifact_paths(tmp_path)) == 3
    assert not clean_artifacts(tmp_path, check=True)
    assert len(artifact_paths(tmp_path)) == 3
    assert not clean_artifacts(tmp_path, check=False)
    assert artifact_paths(tmp_path) == []


def test_tree_comparison_ignores_maintained_files(tmp_path: Path) -> None:
    generated = tmp_path / "generated"
    tracked = tmp_path / "tracked"
    generated.mkdir()
    tracked.mkdir()
    (generated / "README.md").write_text("generated", encoding="utf-8")
    (tracked / "README.md").write_text("maintained", encoding="utf-8")
    (tracked / "crds").mkdir()
    (tracked / "crds/source.yaml").write_text("maintained", encoding="utf-8")
    (generated / "package.py").write_text("same", encoding="utf-8")
    (tracked / "package.py").write_text("same", encoding="utf-8")

    assert compare_trees(generated, tracked) == []

    (tracked / "package.py").write_text("different", encoding="utf-8")
    assert compare_trees(generated, tracked) == ["content differs: package.py"]


def test_crd_utilities_falls_back_to_distribution_name(tmp_path: Path) -> None:
    package = tmp_path / "pulumi_example"
    package.mkdir()
    utilities = package / "_utilities.py"
    utilities.write_text(
        "    pep440_version_string = importlib.metadata.version(root_package)\n",
        encoding="utf-8",
    )

    normalize_crd_utilities(
        package,
        {"packageDir": "pulumi_example", "distributionName": "example"},
    )

    source = utilities.read_text(encoding="utf-8")
    assert "except importlib.metadata.PackageNotFoundError:" in source
    assert 'importlib.metadata.version("example")' in source


def test_terraform_utilities_use_public_package_registration(tmp_path: Path) -> None:
    utilities = tmp_path / "_utilities.py"
    utilities.write_text("async def get_package():\n\treturn await pulumi.runtime.register_package()\n")
    normalize_terraform_utilities(utilities)
    assert utilities.read_text() == (
        "async def get_package():\n    return await pulumi.runtime.register_package()\n"
    )


def test_terraform_utilities_reject_unexpected_generator_shape(tmp_path: Path) -> None:
    utilities = tmp_path / "_utilities.py"
    utilities.write_text("unexpected generator output\n")
    with pytest.raises(GenerationError, match="package registration"):
        normalize_terraform_utilities(utilities)


def test_flux_provider_removal_normalizes_root_initializer(tmp_path: Path) -> None:
    package = tmp_path / "ca4s_flux_crds"
    for relative in ("meta/__init__.py", "source/__init__.py"):
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import pulumi_ca4s_flux_crds\n", encoding="utf-8")
    (package / "provider.py").write_text("provider", encoding="utf-8")
    (package / "pulumi-plugin.json").write_text("{}", encoding="utf-8")
    (package / "__init__.py").write_text(
        "import typing\n"
        "# Export this package's modules as members:\n"
        "from .provider import *\n\n"
        "# Make subpackages available:\n"
        "import pulumi_ca4s_flux_crds\n\n"
        "_utilities.register(\n    resource_modules=\"[]\"\n)\n",
        encoding="utf-8",
    )

    remove_flux_provider_shim(package)

    source = (package / "__init__.py").read_text(encoding="utf-8")
    assert "provider" not in source
    assert "_utilities.register" not in source
    assert "import typing\n\n# Make subpackages available:" in source
    assert "pulumi_ca4s_flux_crds" not in source