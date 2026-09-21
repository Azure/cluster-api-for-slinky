"""Registry endpoints and short-lived publishing sessions."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

from azure.mgmt.core.tools import parse_resource_id
from oras.client import OrasClient
from oras.defaults import default_manifest_accepted_media_types
from pulumi import Input
from requests.exceptions import RequestException

from artifacts import source


class RegistryDestination(TypedDict):
    """Serializable registry coordinates supplied by stack wiring.

    ``server`` is reachable from the build host; ``consumer_server`` identifies
    the same registry from the consumer's network. Both omit URL schemes.
    ``plain_http`` selects transport, not whether to verify TLS certificates.
    An ``acr_resource_id`` enables Entra login for that registry and subscription;
    credentials are obtained at execution time, never stored in this descriptor.
    """

    server: Input[str]
    consumer_server: Input[str]
    plain_http: bool
    acr_resource_id: NotRequired[Input[str]]


@dataclass(frozen=True)
class RegistrySession:
    """Resolved endpoints and publishing operations for one registry session.

    ORAS handles manifest probes and file bundles; Docker and Helm retain their
    native image and chart formats. For ACR, ``registry_session`` owns the temporary
    Docker config shared by these tools. Do not retain this session past that
    context or put its config path into Pulumi outputs.
    """

    server: str
    consumer_server: str
    plain_http: bool
    config_directory: str | None = None

    @contextmanager
    def client(self) -> Iterator[OrasClient]:
        """Isolate repository-scoped bearer tokens and close each SDK HTTP session."""
        client = OrasClient(hostname=self.server, insecure=self.plain_http)
        try:
            # The SDK's default loader merges host credentials; seed only ours instead.
            client.auth._auth_config = (
                json.loads((Path(self.config_directory) / "config.json").read_text())
                if self.config_directory else {"auths": {}}
            )
            yield client
        finally:
            client.session.close()

    def host_ref(self, repository: str, tag: str) -> str:
        return f"{self.server}/{repository}:{tag}"

    def consumer_ref(self, repository: str, tag: str) -> str:
        return f"{self.consumer_server}/{repository}:{tag}"

    def docker_command(self) -> list[str]:
        source.require_binary("docker")
        return ["docker", "--config", self.config_directory] if self.config_directory else ["docker"]

    def registry_flags(self) -> list[str]:
        flags = ["--plain-http"] if self.plain_http else []
        if self.config_directory is not None:
            flags.extend(["--registry-config", f"{self.config_directory}/config.json"])
        return flags

    def manifest_exists(self, repository: str, tag: str) -> bool:
        """Return false only for HTTP 404; auth and transport failures are errors."""
        reference = self.host_ref(repository, tag)
        try:
            with self.client() as client:
                container = client.get_container(reference)
                client.auth.load_configs(container)
                # The SDK lacks an existence API; HEAD preserves status without a download.
                response = client.do_request(
                    f"{client.prefix}://{container.manifest_url()}", "HEAD",
                    headers={"Accept": ", ".join(default_manifest_accepted_media_types)},
                )
        except (RequestException, ValueError):
            raise RuntimeError(f"registry manifest probe failed for {reference}") from None
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            raise RuntimeError(f"registry manifest probe failed for {reference}: HTTP {response.status_code}")
        return True

    def push_image(self, reference: str) -> None:
        source.run([*self.docker_command(), "push", reference])

    def push_files(self, repository: str, tag: str, directory: str, files: list[str]) -> None:
        """Upload files as an OCI bundle, preserving their basenames as layer titles."""
        reference = self.host_ref(repository, tag)
        try:
            with self.client() as client:
                client.push(
                    target=reference,
                    files=[str((Path(directory) / name).resolve()) for name in files],
                    # Build worktrees lie outside cwd; avoid process-wide directory changes.
                    disable_path_validation=True,
                    quiet=True,
                )
        except (RequestException, ValueError):
            raise RuntimeError(f"registry artifact push failed for {reference}") from None

    def push_chart(self, archive: Path, repository_prefix: str) -> None:
        helm = source.require_binary("helm")
        source.run([
            helm, "push", str(archive), f"oci://{self.server}/{repository_prefix}",
            *self.registry_flags(),
        ])


@contextmanager
def registry_session(destination: dict) -> Iterator[RegistrySession]:
    server = source.required_str(destination, "server")
    consumer_server = source.required_str(destination, "consumer_server")
    plain_http = destination["plain_http"]
    if destination.get("acr_resource_id") is None:
        yield RegistrySession(server, consumer_server, plain_http)
        return

    if plain_http:
        raise ValueError("ACR publishing requires HTTPS")
    source.require_binary("az")
    source.require_binary("docker")
    registry = parse_resource_id(source.required_str(destination, "acr_resource_id"))
    result = source.run(
        [
            "az", "acr", "login", "--name", registry["name"],
            "--subscription", registry["subscription"],
            "--expose-token", "--query", "accessToken", "--output", "tsv",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            f"Entra login failed for ACR {server}; sign in with az login and ensure "
            "the publishing identity has AcrPush or equivalent permissions"
        )
    with tempfile.TemporaryDirectory(prefix="ca4s-acr-auth-") as directory:
        result = source.run(
            [
                "docker", "--config", directory, "login", server,
                "--username", "00000000-0000-0000-0000-000000000000", "--password-stdin",
            ],
            stdin=result.stdout.strip(),
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"publisher login failed for ACR {server}")
        yield RegistrySession(server, consumer_server, plain_http, directory)