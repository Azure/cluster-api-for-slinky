#!/usr/bin/env python3

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Regenerate and verify the pinned Pulumi SDKs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "pulumi/sdks/sources.json"
MAINTAINED_ROOT_FILES = {".gitattributes", ".gitignore", "LICENSE", "README.md"}
MAINTAINED_ROOT_DIRECTORIES = {"crds"}
ARTIFACT_DIRECTORY_NAMES = {"build", "dist", "__pycache__"}
ARTIFACT_SUFFIXES = (".egg-info",)
ARTIFACT_FILE_SUFFIXES = (".pyc", ".pyo")


class GenerationError(RuntimeError):
    """A reproducibility check or generation step failed."""


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != 1:
        raise GenerationError("unsupported SDK manifest schemaVersion")
    sdks = manifest.get("sdks")
    if not isinstance(sdks, dict) or not sdks:
        raise GenerationError("SDK manifest must contain a non-empty sdks object")
    for name, sdk in sdks.items():
        required = {"kind", "output", "version", "license"}
        if not isinstance(sdk, dict) or not required.issubset(sdk):
            raise GenerationError(f"SDK {name!r} is missing required fields")
        if sdk["kind"] not in {"crd", "terraform"}:
            raise GenerationError(f"SDK {name!r} has unsupported kind {sdk['kind']!r}")
    return manifest


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(path: Path, expected: str) -> None:
    actual = sha256(path)
    if actual != expected:
        raise GenerationError(
            f"checksum mismatch for {path}: expected {expected}, got {actual}"
        )


def artifact_paths(root: Path) -> list[Path]:
    artifacts: list[Path] = []
    for path in root.rglob("*"):
        if any(parent.name in ARTIFACT_DIRECTORY_NAMES for parent in path.parents):
            continue
        if path.is_dir() and (
            path.name in ARTIFACT_DIRECTORY_NAMES
            or path.name.endswith(ARTIFACT_SUFFIXES)
        ):
            artifacts.append(path)
        elif path.is_file() and path.name.endswith(ARTIFACT_FILE_SUFFIXES):
            artifacts.append(path)
    return sorted(artifacts)


def clean_artifacts(root: Path, check: bool) -> bool:
    artifacts = artifact_paths(root)
    for path in artifacts:
        print(f"{'found' if check else 'remove'}: {path.relative_to(root)}")
        if not check:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    return not artifacts


def _run(command: list[str], cwd: Path = REPO_ROOT) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def verify_crd2pulumi(module: str, version: str) -> str:
    executable = shutil.which("crd2pulumi")
    if executable is None:
        raise GenerationError(
            f"crd2pulumi is required; install with: go install {module}@{version}"
        )
    result = subprocess.run(
        ["go", "version", "-m", executable],
        capture_output=True,
        check=True,
        text=True,
    )
    expected = f"mod\t{module}\t{version}"
    if expected not in result.stdout:
        raise GenerationError(
            f"{executable} is not {module} {version}; install the pinned version"
        )
    return executable


def _download(url: str, destination: Path) -> None:
    with urlopen(url) as response, destination.open("wb") as output:  # noqa: S310
        shutil.copyfileobj(response, output)


def _normalize_pyproject(path: Path, sdk: dict[str, Any]) -> None:
    source = path.read_text(encoding="utf-8")
    if sdk["kind"] == "crd":
        source, count = re.subn(
            r'pulumi-kubernetes==[^"\]]+',
            f"pulumi-kubernetes=={sdk['kubernetesVersion']}",
            source,
        )
        if count != 1:
            raise GenerationError(f"expected one Kubernetes dependency in {path}")
        generated_name = f'pulumi_{sdk["pythonName"]}'
        source = source.replace(
            f'  name = "{generated_name}"',
            f'  name = "{sdk["distributionName"]}"',
            1,
        )
        source, count = re.subn(
            rf'^      {re.escape(generated_name)} = \["py\.typed", "pulumi-plugin\.json"\]$',
            f'      {sdk["packageDir"]} = ["py.typed"]',
            source,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise GenerationError(f"expected generated package data in {path}")
        if sdk["packageDir"] != sdk["distributionName"]:
            source += (
                "    [tool.setuptools.packages.find]\n"
                f'      include = ["{sdk["packageDir"]}*"]\n'
            )
    if "[project.license]" not in source:
        marker = "  version = "
        line = next((line for line in source.splitlines() if line.startswith(marker)), None)
        if line is None:
            raise GenerationError(f"cannot locate project version in {path}")
        source = source.replace(
            f"{line}\n",
            f'{line}\n  [project.license]\n    text = "{sdk["license"]}"\n',
            1,
        )
    path.write_text(source, encoding="utf-8")


def _normalize_terraform_readme(path: Path, sdk: dict[str, Any]) -> None:
    if sdk["license"] != "MIT":
        return
    source = path.read_text(encoding="utf-8")
    old = "distributed under [MPL 2.0](https://www.mozilla.org/en-US/MPL/2.0/)."
    new = f'distributed under the [MIT License]({sdk["licenseUrl"]}).'
    if source.count(old) != 1:
        raise GenerationError(f"expected one generated MPL license claim in {path}")
    path.write_text(source.replace(old, new), encoding="utf-8")


def _remove_flux_provider_shim(package: Path) -> None:
    (package / "provider.py").unlink()
    (package / "pulumi-plugin.json").unlink()
    old_name = f"pulumi_{package.name}"
    for path in (package / "__init__.py", package / "meta/__init__.py", package / "source/__init__.py"):
        source = path.read_text(encoding="utf-8").replace(old_name, package.name)
        if path == package / "__init__.py":
            source = re.sub(
                r"# Export this package's modules as members:\nfrom \.provider import \*\n\n",
                "\n",
                source,
                count=1,
            )
            source = re.sub(r"\n_utilities\.register\(.*\)\n$", "\n", source, flags=re.DOTALL)
        path.write_text(source, encoding="utf-8")


def _normalize_crd_utilities(package: Path, sdk: dict[str, Any]) -> None:
    if sdk["packageDir"] == sdk["distributionName"]:
        return
    path = package / "_utilities.py"
    source = path.read_text(encoding="utf-8")
    old = "    pep440_version_string = importlib.metadata.version(root_package)\n"
    new = (
        "    try:\n"
        "        pep440_version_string = importlib.metadata.version(root_package)\n"
        "    except importlib.metadata.PackageNotFoundError:\n"
        f'        pep440_version_string = importlib.metadata.version("{sdk["distributionName"]}")\n'
    )
    if source.count(old) != 1:
        raise GenerationError(f"expected generated metadata lookup in {path}")
    path.write_text(source.replace(old, new), encoding="utf-8")


def _generate_terraform(
    sdk: dict[str, Any], destination: Path, bridge: dict[str, str]
) -> None:
    pulumi = shutil.which("pulumi")
    if pulumi is None:
        raise GenerationError("pulumi CLI is required to generate Terraform SDKs")
    staging = destination.parent / "terraform-output"
    _run(
        [
            pulumi,
            "--non-interactive",
            "package",
            "gen-sdk",
            f'{bridge["package"]}@{bridge["version"]}',
            "--language",
            "python",
            "--out",
            str(staging),
            sdk["provider"],
            sdk["version"],
        ]
    )
    shutil.copytree(staging / "python", destination)


def _generate_crd(
    name: str,
    sdk: dict[str, Any],
    destination: Path,
    crd2pulumi: str,
    temp_root: Path,
) -> None:
    source = temp_root / f"{name}.yaml"
    if "source" in sdk:
        shutil.copyfile(REPO_ROOT / sdk["source"], source)
    else:
        _download(sdk["sourceUrl"], source)
    verify_checksum(source, sdk["sha256"])
    _run(
        [
            crd2pulumi,
            "--pythonPath",
            str(destination),
            "--pythonName",
            sdk["pythonName"],
            "--version",
            sdk["version"],
            "--force",
            str(source),
        ]
    )
    generated_package = destination / f'pulumi_{sdk["pythonName"]}'
    package = destination / sdk["packageDir"]
    if generated_package != package:
        generated_package.rename(package)
    if sdk.get("removeProviderShim"):
        _remove_flux_provider_shim(package)
    _normalize_crd_utilities(package, sdk)


def _generated_files(root: Path) -> dict[Path, bytes]:
    result: dict[Path, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if len(relative.parts) == 1 and relative.name in MAINTAINED_ROOT_FILES:
            continue
        if relative.parts[0] in MAINTAINED_ROOT_DIRECTORIES:
            continue
        if any(part in ARTIFACT_DIRECTORY_NAMES for part in relative.parts):
            continue
        if any(part.endswith(ARTIFACT_SUFFIXES) for part in relative.parts):
            continue
        if relative.name.endswith(ARTIFACT_FILE_SUFFIXES):
            continue
        result[relative] = path.read_bytes()
    return result


def compare_trees(generated: Path, tracked: Path) -> list[str]:
    generated_files = _generated_files(generated)
    tracked_files = _generated_files(tracked)
    differences = [
        f"missing generated file: {path}"
        for path in sorted(tracked_files.keys() - generated_files.keys())
    ]
    differences.extend(
        f"untracked generated file: {path}"
        for path in sorted(generated_files.keys() - tracked_files.keys())
    )
    differences.extend(
        f"content differs: {path}"
        for path in sorted(generated_files.keys() & tracked_files.keys())
        if generated_files[path] != tracked_files[path]
    )
    return differences


def _replace_generated_files(generated: Path, tracked: Path) -> None:
    generated_files = _generated_files(generated)
    tracked_files = _generated_files(tracked)
    for relative in tracked_files.keys() - generated_files.keys():
        (tracked / relative).unlink()
    for relative, content in generated_files.items():
        destination = tracked / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def generate_sdk(
    name: str, manifest: dict[str, Any], check: bool, temp_root: Path
) -> bool:
    sdk = manifest["sdks"][name]
    generated = temp_root / sdk["output"]
    generated.parent.mkdir(parents=True, exist_ok=True)
    if sdk["kind"] == "terraform":
        _generate_terraform(sdk, generated, manifest["tools"]["terraformBridge"])
    else:
        tool = manifest["tools"]["crd2pulumi"]
        executable = verify_crd2pulumi(tool["module"], tool["version"])
        _generate_crd(name, sdk, generated, executable, temp_root)
    _normalize_pyproject(generated / "pyproject.toml", sdk)
    if sdk["kind"] == "terraform":
        package = next(generated.glob("pulumi_*/README.md"), None)
        if package is None:
            raise GenerationError(f"generated README not found for {name}")
        _normalize_terraform_readme(package, sdk)

    if sdk["kind"] == "crd":
        patcher = REPO_ROOT / "scripts/patch_crd_sdk_provider_defaults.py"
        _run(
            [
                sys.executable,
                str(patcher),
                "--root",
                str(temp_root),
                "--target",
                name,
            ]
        )

    tracked = REPO_ROOT / sdk["output"]
    differences = compare_trees(generated, tracked)
    if differences and check:
        for difference in differences:
            print(f"{name}: {difference}", file=sys.stderr)
        return False
    if differences:
        _replace_generated_files(generated, tracked)
        print(f"updated: {name}")
    else:
        print(f"unchanged: {name}")
    return True


def verify_manifest(manifest: dict[str, Any]) -> bool:
    valid = True
    for name, sdk in manifest["sdks"].items():
        output = REPO_ROOT / sdk["output"]
        if not output.is_dir():
            print(f"missing SDK directory: {sdk['output']}", file=sys.stderr)
            valid = False
        if "source" in sdk:
            try:
                verify_checksum(REPO_ROOT / sdk["source"], sdk["sha256"])
            except GenerationError as error:
                print(f"{name}: {error}", file=sys.stderr)
                valid = False
    return valid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify", help="validate the manifest and local inputs")
    clean_parser = subparsers.add_parser("clean", help="remove disposable SDK artifacts")
    clean_parser.add_argument("--check", action="store_true")
    generate_parser = subparsers.add_parser("generate", help="regenerate selected SDKs")
    selection = generate_parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--sdk", action="append", dest="sdks")
    selection.add_argument("--all", action="store_true")
    generate_parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    try:
        manifest = load_manifest()
        if args.command == "verify":
            return 0 if verify_manifest(manifest) else 1
        if args.command == "clean":
            clean = clean_artifacts(REPO_ROOT / "pulumi/sdks", args.check)
            return 0 if clean or not args.check else 1

        selected = list(manifest["sdks"]) if args.all else args.sdks
        unknown = set(selected) - manifest["sdks"].keys()
        if unknown:
            raise GenerationError(f"unknown SDK(s): {', '.join(sorted(unknown))}")
        with tempfile.TemporaryDirectory(prefix="ca4s-sdk-") as temporary:
            temp_root = Path(temporary)
            success = True
            for name in selected:
                success = generate_sdk(name, manifest, args.check, temp_root) and success
        return 0 if success else 1
    except (GenerationError, OSError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())