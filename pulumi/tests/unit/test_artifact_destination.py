from pathlib import Path
import base64
import hashlib
import json
import re
import subprocess
from urllib.parse import parse_qs, urlparse

import pytest
import responses
from requests.exceptions import ConnectionError
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from artifacts.destination import RegistrySession, registry_session
from artifacts import destination


@pytest.fixture(autouse=True)
def skip_sdk_retry_delay(monkeypatch):
    monkeypatch.setattr("oras.decorator.time.sleep", lambda _: None)


@pytest.mark.parametrize("plain_http", [False, True])
def test_format_publishers_use_destination_transport(monkeypatch, plain_http):
    calls = []
    pushes = []
    monkeypatch.setattr(destination.source, "require_binary", lambda name: name)
    monkeypatch.setattr(destination.source, "run", lambda command, **kwargs: calls.append((command, kwargs)))

    def push(client, **kwargs):
        assert client.prefix == ("http" if plain_http else "https")
        pushes.append(kwargs)

    monkeypatch.setattr(destination.OrasClient, "push", push)
    session = RegistrySession("publish:5000", "consumer:5000", plain_http)
    session.push_image(session.host_ref("image", "tag"))
    session.push_files("bundle", "tag", "/tmp/build", ["metadata.yaml"])
    session.push_chart(Path("/tmp/chart.tgz"), "charts")
    assert session.consumer_ref("bundle", "tag") == "consumer:5000/bundle:tag"
    assert calls[0][0] == ["docker", "push", "publish:5000/image:tag"]
    assert pushes == [{
        "target": "publish:5000/bundle:tag", "files": ["/tmp/build/metadata.yaml"],
        "disable_path_validation": True, "quiet": True,
    }]
    for command, _ in calls[1:]:
        assert ("--plain-http" in command) is plain_http
        assert "--registry-config" not in command
    assert "oci://publish:5000/charts" in calls[1][0]


@pytest.mark.parametrize("failure", [None, "az", "docker"])
def test_acr_session_uses_ephemeral_entra_auth(monkeypatch, failure):
    calls = []
    directories = []
    monkeypatch.setattr(destination.source, "require_binary", lambda name: name)

    def run(command, **kwargs):
        calls.append(command)
        assert "sensitive-token" not in command
        assert kwargs["check"] is False
        if command[0] == "az":
            assert command == [
                "az", "acr", "login", "--name", "images", "--subscription", "sub",
                "--expose-token", "--query", "accessToken", "--output", "tsv",
            ]
        else:
            assert kwargs["stdin"] == "sensitive-token"
            assert command[-3:] == ["--username", "00000000-0000-0000-0000-000000000000", "--password-stdin"]
            directories.append(Path(command[2]))
        return subprocess.CompletedProcess(
            command, int(command[0] == failure), stdout="sensitive-token\n", stderr="sensitive-token",
        )

    monkeypatch.setattr(destination.source, "run", run)
    config = {
        "server": "images.azurecr.io", "consumer_server": "images.azurecr.io", "plain_http": False,
        "acr_resource_id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ContainerRegistry/registries/images",
    }

    def login():
        with registry_session(config) as session:
            assert Path(session.config_directory).is_dir()
            assert session.registry_flags() == ["--registry-config", f"{session.config_directory}/config.json"]
            assert "--config" in session.docker_command()
            assert session.acr_subscription_id == "sub"
            assert session.host_ref("image", "tag") == session.consumer_ref("image", "tag")

    if failure:
        with pytest.raises(RuntimeError, match="login failed") as error:
            login()
        assert "sensitive-token" not in str(error.value)
    else:
        login()
        login()
        assert sum(command[0] == "az" for command in calls) == 2
    assert all(not directory.exists() for directory in directories)


@pytest.mark.parametrize("error", [None, ResourceNotFoundError, HttpResponseError])
def test_acr_probe_uses_azure_sdk_token_exchange(monkeypatch, error):
    calls = []

    class Credential:
        def __init__(self, **kwargs):
            assert kwargs == {"subscription": "sub"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("credential-closed")

    class Client:
        def __init__(self, endpoint, credential):
            assert endpoint == "https://images.azurecr.io"
            assert isinstance(credential, Credential)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("client-closed")

        def get_manifest_properties(self, repository, tag):
            calls.append((repository, tag))
            if error is not None:
                raise error("sensitive details")

    monkeypatch.setattr(destination, "AzureCliCredential", Credential)
    monkeypatch.setattr(destination, "ContainerRegistryClient", Client)
    session = RegistrySession("images.azurecr.io", "images.azurecr.io", False, acr_subscription_id="sub")
    if error is HttpResponseError:
        with pytest.raises(RuntimeError, match="manifest probe failed") as failure:
            session.manifest_exists("image", "tag")
        assert "sensitive" not in str(failure.value)
    else:
        assert session.manifest_exists("image", "tag") is (error is None)
    assert calls == [("image", "tag"), "client-closed", "credential-closed"]


@pytest.mark.parametrize("plain_http", [False, True])
@pytest.mark.parametrize("status", [200, 404, 401, 403, 500])
@responses.activate
def test_sdk_probe_only_treats_404_as_absent(plain_http, status):
    session = RegistrySession("registry.example:5002", "registry:5000", plain_http)
    protocol = "http" if plain_http else "https"
    responses.head(f"{protocol}://registry.example:5002/v2/image/manifests/tag", status=status)
    if status == 404:
        assert not session.manifest_exists("image", "tag")
    elif status == 200:
        assert session.manifest_exists("image", "tag")
    else:
        with pytest.raises(RuntimeError, match="manifest probe failed"):
            session.manifest_exists("image", "tag")
    assert "application/vnd.oci.image.index.v1+json" in responses.calls[0].request.headers["Accept"]


@responses.activate
def test_sdk_probe_propagates_connection_failure_without_response_details():
    responses.head("https://registry.example/v2/image/manifests/tag", body=ConnectionError("sensitive details"))
    with pytest.raises(RuntimeError, match="manifest probe failed") as error:
        RegistrySession("registry.example", "registry.example", False).manifest_exists("image", "tag")
    assert "sensitive details" not in str(error.value)


@responses.activate
def test_sdk_uses_isolated_credentials_and_repository_scoped_tokens(monkeypatch, tmp_path):
    username = "00000000-0000-0000-0000-000000000000"
    auth = base64.b64encode(f"{username}:entra-token".encode()).decode()
    (tmp_path / "config.json").write_text(json.dumps({
        "auths": {"images.azurecr.io": {"auth": auth}},
    }))
    monkeypatch.setattr(
        "oras.auth.utils.load_configs",
        lambda *args: pytest.fail("must not merge the host's Docker credentials"),
    )
    scopes = []

    def issue_token(request):
        assert request.headers["Authorization"] == f"Basic {auth}"
        scope = parse_qs(urlparse(request.url).query)["scope"][0]
        scopes.append(scope)
        return 200, {"Content-Type": "application/json"}, json.dumps({"token": f"token-for-{scope}"})

    responses.add_callback(responses.GET, "https://images.azurecr.io/oauth2/token", callback=issue_token)
    session = RegistrySession("images.azurecr.io", "images.azurecr.io", False, str(tmp_path))
    for repository in ("image", "charts/slurm"):
        scope = f"repository:{repository}:pull"
        url = f"https://images.azurecr.io/v2/{repository}/manifests/tag"
        responses.head(url, status=401, headers={
            "Www-Authenticate": (
                'Bearer realm="https://images.azurecr.io/oauth2/token",'
                f'service="images.azurecr.io",scope="{scope}"'
            ),
        })
        responses.head(url, status=200, match=[responses.matchers.header_matcher({
            "Authorization": f"Bearer token-for-{scope}",
        })])
        assert session.manifest_exists(repository, "tag")
    assert scopes == ["repository:image:pull", "repository:charts/slurm:pull"]


@pytest.mark.parametrize("plain_http", [False, True])
@pytest.mark.parametrize("manifest_status", [201, 400])
@responses.activate
def test_sdk_push_uploads_capz_files_and_oci_manifest(tmp_path, plain_http, manifest_status):
    files = {"metadata.yaml": b"metadata", "infrastructure-components.yaml": b"components"}
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
    protocol = "http" if plain_http else "https"
    base = f"{protocol}://registry.example/v2/capz/cluster-api-provider-azure"
    uploaded = {}
    manifests = []
    responses.head(re.compile(re.escape(base) + r"/blobs/sha256:[0-9a-f]+$"), status=404)
    responses.post(f"{base}/blobs/uploads/", status=202, headers={"Location": f"{base}/blobs/uploads/session"})

    def upload_blob(request):
        digest = parse_qs(urlparse(request.url).query)["digest"][0]
        assert digest == "sha256:" + hashlib.sha256(request.body).hexdigest()
        uploaded[digest] = request.body
        return 201, {}, ""

    def upload_manifest(request):
        assert request.headers["Content-Type"] == "application/vnd.oci.image.manifest.v1+json"
        manifests.append(json.loads(request.body))
        return manifest_status, {}, ""

    responses.add_callback(responses.PUT, f"{base}/blobs/uploads/session", callback=upload_blob)
    responses.add_callback(responses.PUT, f"{base}/manifests/tag", callback=upload_manifest)
    session = RegistrySession("registry.example", "consumer.example", plain_http)
    if manifest_status == 201:
        session.push_files("capz/cluster-api-provider-azure", "tag", str(tmp_path), list(files))
    else:
        with pytest.raises(RuntimeError, match="artifact push failed"):
            session.push_files("capz/cluster-api-provider-azure", "tag", str(tmp_path), list(files))
    manifest = manifests[0]
    assert manifest["schemaVersion"] == 2
    assert len(manifest["layers"]) == 2
    for layer in manifest["layers"]:
        name = layer["annotations"]["org.opencontainers.image.title"]
        assert uploaded[layer["digest"]] == files[name]
        assert layer["size"] == len(files[name])
    assert uploaded[manifest["config"]["digest"]] == b"{}"
    assert str(tmp_path) not in json.dumps(manifest)


def test_acr_never_authenticates_over_plain_http(monkeypatch):
    monkeypatch.setattr(destination.source, "run", lambda *args, **kwargs: pytest.fail("must reject before login"))
    with pytest.raises(ValueError, match="HTTPS"):
        with registry_session({
            "server": "images.azurecr.io", "consumer_server": "images.azurecr.io",
            "plain_http": True, "acr_resource_id": "registry-id",
        }):
            pytest.fail("must not open an insecure authenticated session")