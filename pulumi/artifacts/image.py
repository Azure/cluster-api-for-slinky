"""Build a custom Docker image and publish it to a registry destination."""

from __future__ import annotations

from artifacts import source
from artifacts.destination import RegistrySession


def artifact_tags(options: dict, commit: str) -> dict[str, str]:
    return {source.required_str(options, "image_name"): source.source_tag(commit)}


def build_and_publish(worktree: str, options: dict, tags: dict[str, str], session: RegistrySession) -> None:
    repository, tag = next(iter(tags.items()))
    reference = session.host_ref(repository, tag)
    _build_image(worktree, reference, options, session.docker_command())
    session.push_image(reference)


def _build_image(worktree: str, reference: str, props: dict, docker_command: list[str]) -> None:
    command = [*docker_command, "build"]
    build_args = props.get("build_args")
    if build_args is None:
        build_args = {"ARCH": "amd64"}
    for key, value in sorted(build_args.items()):
        command.extend(["--build-arg", f"{key}={value}"])
    if props.get("target") is not None:
        command.extend(["--target", source.required_str(props, "target")])
    source.run([*command, "-t", reference, worktree])

