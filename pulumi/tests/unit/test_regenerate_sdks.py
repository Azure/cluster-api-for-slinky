# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Tests for the manifest-driven Pulumi SDK regeneration workflow."""

from __future__ import annotations

import json
from pathlib import Path
import runpy

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
_GENERATOR = runpy.run_path(str(_REPO_ROOT / "scripts/regenerate_sdks.py"))
GenerationError = _GENERATOR["GenerationError"]
artifact_paths = _GENERATOR["artifact_paths"]
clean_artifacts = _GENERATOR["clean_artifacts"]
compare_trees = _GENERATOR["compare_trees"]
load_manifest = _GENERATOR["load_manifest"]
normalize_crd_utilities = _GENERATOR["_normalize_crd_utilities"]
remove_flux_provider_shim = _GENERATOR["_remove_flux_provider_shim"]
verify_checksum = _GENERATOR["verify_checksum"]


def test_committed_manifest_is_valid() -> None:
    manifest = load_manifest()

    assert set(manifest["sdks"]) == {"awx", "capi-core", "flux", "gitea", "local"}


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