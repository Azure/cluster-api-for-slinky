"""Pre-pull bootstrap images into kind nodes before Helm waits begin."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from typing import Optional

from pulumi import Input, Output, ResourceOptions
from pulumi.dynamic import (
    CheckFailure,
    CheckResult,
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
    UpdateResult,
)

_RESOURCE_TYPE = "ca4s:local:KindImageCache"


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(
            f"required binary '{name}' not found in PATH; install it before running pulumi"
        )
    return path


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({' '.join(cmd)}): {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def _cluster_name_candidates(cluster_name: str) -> tuple[str, ...]:
    names = [cluster_name]
    if cluster_name.startswith("kind-"):
        names.append(cluster_name.removeprefix("kind-"))
    else:
        names.append(f"kind-{cluster_name}")
    return tuple(dict.fromkeys(name for name in names if name))


def _kind_nodes(cluster_name: str) -> tuple[str, list[str]]:
    _require_binary("docker")
    for candidate in _cluster_name_candidates(cluster_name):
        result = _run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=io.x-k8s.kind.cluster={candidate}",
                "--format",
                "{{.Names}}",
            ],
            check=False,
        )
        nodes = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
        if nodes:
            return candidate, nodes
    raise RuntimeError(f"no running kind node containers found for {cluster_name!r}")


def _pull_images(cluster_name: str, images: Sequence[str]) -> dict[str, object]:
    kind_cluster_name, nodes = _kind_nodes(cluster_name)
    pulled_images: list[str] = []
    for node in nodes:
        for image in images:
            _run(["docker", "exec", node, "crictl", "pull", image])
            pulled_images.append(f"{node}:{image}")

    return {
        "cluster_name": cluster_name,
        "kind_cluster_name": kind_cluster_name,
        "images": list(images),
        "node_names": list(nodes),
        "pulled_images": pulled_images,
    }


class _KindImageCacheProvider(ResourceProvider):
    def check(self, _olds: dict, news: dict) -> CheckResult:
        images = tuple(str(image).strip() for image in news.get("images") or ())
        failures: list[CheckFailure] = []
        if not str(news.get("cluster_name") or "").strip():
            failures.append(CheckFailure("cluster_name", "cluster_name is required"))
        if not images:
            failures.append(CheckFailure("images", "at least one image is required"))
        if any(not image for image in images):
            failures.append(CheckFailure("images", "image names must be non-empty"))

        inputs = dict(news)
        inputs["images"] = images
        return CheckResult(inputs=inputs, failures=failures)

    def create(self, props: dict) -> CreateResult:
        outs = _pull_images(str(props["cluster_name"]), tuple(props["images"]))
        return CreateResult(
            id_=f"{outs['kind_cluster_name']}/bootstrap-image-cache",
            outs=outs,
        )

    def diff(self, _id: str, olds: dict, news: dict) -> DiffResult:
        return DiffResult(
            changes=(
                olds.get("cluster_name") != news.get("cluster_name")
                or tuple(olds.get("images") or ()) != tuple(news.get("images") or ())
            )
        )

    def update(self, _id: str, _olds: dict, news: dict) -> UpdateResult:
        return UpdateResult(
            outs=_pull_images(str(news["cluster_name"]), tuple(news["images"]))
        )

    def delete(self, _id: str, _props: dict) -> None:
        return None

    def read(self, id_: str, props: dict) -> ReadResult:
        try:
            _, nodes = _kind_nodes(str(props["cluster_name"]))
        except RuntimeError:
            return ReadResult(id_=None, outs={})
        outs = dict(props)
        outs["node_names"] = nodes
        return ReadResult(id_=id_, outs=outs)


class KindImageCache(Resource):
    """Pull images into all kind nodes so later pod scheduling is cache-hot."""

    kind_cluster_name: Output[str]
    node_names: Output[list[str]]
    pulled_images: Output[list[str]]

    def __init__(
        self,
        name: str,
        *,
        cluster_name: Input[str],
        images: Input[Sequence[str]],
        opts: Optional[ResourceOptions] = None,
    ) -> None:
        super().__init__(
            _KindImageCacheProvider(),
            name,
            {
                "cluster_name": cluster_name,
                "images": images,
                "kind_cluster_name": None,
                "node_names": None,
                "pulled_images": None,
            },
            opts,
        )