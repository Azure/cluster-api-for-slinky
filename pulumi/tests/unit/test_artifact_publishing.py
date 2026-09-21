from contextlib import contextmanager
from pathlib import Path
import base64
import json
import subprocess
from types import SimpleNamespace

import dill
import pulumi
import pytest
from pulumi.dynamic.dynamic import serialize_provider
from pulumi.runtime.rpc import unwrap_rpc_secret

from artifacts import capz, image, slinky, source
from artifacts.destination import RegistrySession
from oras.client import OrasClient
from artifacts.publishing import PublishedArtifacts, _ArtifactProvider


_COMMIT = "1234567890abcdef1234567890abcdef12345678"
_ACR_ID = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ContainerRegistry/registries/images"


def test_arbitrary_callbacks_survive_provider_serialization(monkeypatch, tmp_path):
    def plan(options, commit):
        return {options["repository"]: f"custom-{commit[:8]}"}

    def build(worktree, options, tags, session):
        assert tags == {"custom/output": "custom-12345678"}
        (Path(worktree) / "built").write_text(session.server)

    provider = _ArtifactProvider(plan=plan, build=build)
    restored = dill.loads(base64.b64decode(serialize_provider(provider)))
    monkeypatch.setattr(source, "resolve_source_commit", lambda *args: _COMMIT)
    monkeypatch.setattr(RegistrySession, "manifest_exists", lambda *args: False)

    @contextmanager
    def worktree(*args, **kwargs):
        yield str(tmp_path)

    monkeypatch.setattr(source, "detached_worktree", worktree)
    props = {
        "source_path": "/src/repo", "source_ref": "feature", "build_options": {"repository": "custom/output"},
        "destination": {"server": "localhost:5002", "consumer_server": "registry:5000", "plain_http": True},
    }
    result = restored.create(props)
    assert result.id == "registry:5000/custom/output:custom-12345678"
    assert result.outs["artifact_refs"] == {"custom/output": result.id}
    assert (tmp_path / "built").read_text() == "localhost:5002"
    assert "recipe" not in result.outs
    assert "plan" not in result.outs
    assert "build" not in result.outs


def test_changing_injected_callbacks_triggers_provider_diff():
    def plan(options, commit):
        return {"custom/output": commit}

    def build(worktree, options, tags, session):
        pass

    def changed_plan(options, commit):
        return {"custom/other": commit}

    def changed_build(worktree, options, tags, session):
        raise RuntimeError("different implementation")

    provider = _ArtifactProvider(plan=plan, build=build)
    old = {"__provider": serialize_provider(provider)}
    assert not provider.diff("existing", old, old).changes
    for updated in (
        _ArtifactProvider(plan=changed_plan, build=build),
        _ArtifactProvider(plan=plan, build=changed_build),
    ):
        assert updated.diff("existing", old, {"__provider": serialize_provider(updated)}).changes


@pytest.mark.parametrize("recipe", ["image", "capz", "slinky"])
@pytest.mark.parametrize("exists", [False, True])
def test_generic_lifecycle_uses_recipe_functions(monkeypatch, recipe, exists):
    module = {"image": image, "capz": capz, "slinky": slinky}[recipe]
    builds = []
    probes = []
    worktrees = []
    monkeypatch.setattr(source, "resolve_source_commit", lambda *args: _COMMIT)

    @contextmanager
    def worktree(*args, **kwargs):
        worktrees.append("enter")
        try:
            yield "/tmp/build"
        finally:
            worktrees.append("exit")

    def manifest(session, repository, tag):
        probes.append((repository, tag))
        return exists

    monkeypatch.setattr(source, "detached_worktree", worktree)
    monkeypatch.setattr(RegistrySession, "manifest_exists", manifest)
    monkeypatch.setattr(module, "build_and_publish", lambda *args: builds.append(args))
    props = {
        "source_path": "/src/repo", "source_ref": "feature",
        "build_options": {"image_name": "custom/image"} if recipe == "image" else None,
        "destination": {"server": "localhost:5002", "consumer_server": "registry:5000", "plain_http": True},
    }
    provider = _ArtifactProvider(plan=module.artifact_tags, build=module.build_and_publish)
    result = provider.create(props)
    assert result.outs["artifact_tags"] == module.artifact_tags(props["build_options"] or {}, _COMMIT)
    assert result.outs["built"] is not exists
    assert len(builds) == int(not exists)
    assert worktrees == ([] if exists else ["enter", "exit"])
    assert all(reference.startswith("registry:5000/") for reference in result.outs["artifact_refs"].values())
    assert "/tmp/build" not in repr(result.outs)
    assert not provider.diff(result.id, result.outs, props).changes
    assert provider.diff(result.id, props, {**props, "build_options": {"target": "other"}}).changes
    assert provider.read(result.id, result.outs).id == (result.id if exists else None)
    if exists:
        assert set(probes) == set(result.outs["artifact_tags"].items())


def test_generic_update_rebuilds_when_one_chart_is_missing(monkeypatch):
    builds = []
    probes = []
    monkeypatch.setattr(source, "resolve_source_commit", lambda *args: _COMMIT)

    @contextmanager
    def worktree(*args, **kwargs):
        yield "/tmp/build"

    def manifest(session, repository, tag):
        probes.append(repository)
        return repository != "charts/slurm"

    monkeypatch.setattr(source, "detached_worktree", worktree)
    monkeypatch.setattr(RegistrySession, "manifest_exists", manifest)
    monkeypatch.setattr(slinky, "build_and_publish", lambda *args: builds.append(args))
    props = {
        "source_path": "/src/repo", "source_ref": "feature",
        "destination": {"server": "localhost:5002", "consumer_server": "registry:5000", "plain_http": True},
    }
    provider = _ArtifactProvider(plan=slinky.artifact_tags, build=slinky.build_and_publish)
    updated = provider.update("existing-charts", props, props)
    assert probes == [f"charts/{name}" for name in slinky._CHART_NAMES]
    assert len(builds) == 1
    assert len(updated.outs["artifact_refs"]) == 3
    assert updated.outs["built"] is True
    assert provider.read("existing-charts", updated.outs).id is None


def test_generic_build_failure_cleans_worktree_and_fails_update(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(source, "resolve_source_commit", lambda *args: _COMMIT)
    monkeypatch.setattr(source.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path / "build"))
    monkeypatch.setattr(source, "run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(RegistrySession, "manifest_exists", lambda *args: False)

    def fail_build(*args):
        raise RuntimeError("build failed")

    monkeypatch.setattr(capz, "build_and_publish", fail_build)
    props = {
        "source_path": "/src/repo", "source_ref": "feature",
        "destination": {"server": "localhost:5002", "consumer_server": "registry:5000", "plain_http": True},
    }
    with pytest.raises(RuntimeError, match="build failed"):
        _ArtifactProvider(plan=capz.artifact_tags, build=capz.build_and_publish).update("existing", props, props)
    assert calls[-1] == ["git", "-C", "/src/repo", "worktree", "remove", "--force", str(tmp_path / "build")]


def test_generic_refresh_preserves_state_on_registry_error(monkeypatch):
    monkeypatch.setattr(source, "resolve_source_commit", lambda *args: _COMMIT)

    def fail_probe(*args):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(RegistrySession, "manifest_exists", fail_probe)
    props = {
        "source_path": "/src/repo", "source_ref": "feature", "artifact_refs": {"bundle": "registry/bundle:old"},
        "destination": {"server": "localhost:5002", "consumer_server": "registry:5000", "plain_http": True},
    }
    result = _ArtifactProvider(plan=capz.artifact_tags, build=capz.build_and_publish).read("existing", props)
    assert result.id == "existing"
    assert result.outs == props


@pytest.mark.parametrize("recipe,repository", [
    ("image", "custom/image"),
    ("capz", "capz/cluster-api-provider-azure"),
])
@pytest.mark.parametrize("server,consumer,plain_http", [
    ("localhost:5002", "custom-registry:5000", True),
    ("images.azurecr.io", "images.azurecr.io", False),
])
@pytest.mark.parametrize("exists", [False, True])
def test_recipes_publish_to_either_destination(
    monkeypatch, tmp_path, recipe, repository, server, consumer, plain_http, exists,
):
    calls = []
    worktrees = []
    monkeypatch.setattr(source, "resolve_source_commit", lambda *args: _COMMIT)
    monkeypatch.setattr(source, "require_binary", lambda name: name)

    @contextmanager
    def worktree(*args, **kwargs):
        worktrees.append("enter")
        try:
            yield str(tmp_path)
        finally:
            worktrees.append("exit")

    def run(command, **kwargs):
        if command[0] == "az":
            return subprocess.CompletedProcess(command, 0, stdout="test-token")
        if "login" in command:
            assert kwargs["stdin"] == "test-token"
            (Path(command[2]) / "config.json").write_text(json.dumps({"auths": {}}))
            return subprocess.CompletedProcess(command, 0)
        assert worktrees[-1] == "enter"
        calls.append(command)
        if command[0] == "make":
            (tmp_path / "out").mkdir(exist_ok=True)
            for name in capz._ARTIFACT_FILES:
                (tmp_path / "out" / name).touch()

    def manifest(session, name, tag):
        assert session.server == server
        assert name == repository
        assert tag == "source-1234567890ab"
        return exists

    monkeypatch.setattr(source, "detached_worktree", worktree)
    monkeypatch.setattr(source, "run", run)
    monkeypatch.setattr(RegistrySession, "manifest_exists", manifest)

    def push(client, **kwargs):
        assert worktrees[-1] == "enter"
        assert kwargs["target"] == f"{server}/{repository}:source-1234567890ab"
        assert all(Path(name).is_file() for name in kwargs["files"])
        calls.append(["sdk-push", kwargs["target"]])

    monkeypatch.setattr(OrasClient, "push", push)
    props = {
        "source_path": "/src/repo", "source_ref": "feature",
        "build_options": {"image_name": repository} if recipe == "image" else None,
        "destination": {"server": server, "consumer_server": consumer, "plain_http": plain_http},
    }
    if not plain_http:
        props["destination"]["acr_resource_id"] = _ACR_ID
    module = image if recipe == "image" else capz
    provider = _ArtifactProvider(plan=module.artifact_tags, build=module.build_and_publish)
    result = provider.create(props)
    assert result.id == f"{consumer}/{repository}:source-1234567890ab"
    assert result.outs["artifact_refs"][repository] == result.id
    assert result.outs["built"] is not exists
    assert "test-token" not in repr(result.outs)
    assert str(tmp_path) not in repr(result.outs)
    assert worktrees == ([] if exists else ["enter", "exit"])
    assert len(calls) == (0 if exists else 2)
    if not exists:
        assert calls[-1][0] == ("docker" if recipe == "image" else "sdk-push")
        assert f"{server}/{repository}:source-1234567890ab" in calls[-1]
    assert provider.diff(result.id, props, {**props, "destination": {**props["destination"], "server": "other"}}).changes
    assert not provider.diff(result.id, result.outs, props).changes
    assert provider.read(result.id, result.outs).id == (result.id if exists else None)


@pytest.mark.parametrize("build_args", [{}, {"ARCH": "arm64"}])
def test_image_build_honors_custom_target_and_args(monkeypatch, build_args):
    calls = []
    monkeypatch.setattr(source, "run", lambda command: calls.append(command))
    image._build_image("/tmp/tree", "build:tag", {"target": "custom-stage", "build_args": build_args}, ["docker"])
    flags = ["--build-arg", "ARCH=arm64"] if build_args else []
    assert calls == [["docker", "build", *flags, "--target", "custom-stage", "-t", "build:tag", "/tmp/tree"]]


def test_capz_build_requires_generated_files(monkeypatch, tmp_path):
    monkeypatch.setattr(source, "require_binary", lambda name: name)
    monkeypatch.setattr(source, "run", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="infrastructure-components.yaml"):
        capz._build_bundle(str(tmp_path))


@pytest.mark.parametrize("plain_http", [False, True])
@pytest.mark.parametrize("exists", [False, True])
def test_slinky_charts_use_destination_without_changing_recipe(monkeypatch, tmp_path, plain_http, exists):
    calls = []
    monkeypatch.setattr(source, "resolve_source_commit", lambda *args: _COMMIT)
    monkeypatch.setattr(source, "require_binary", lambda name: name)

    def run(command, **kwargs):
        if command[0] == "az":
            return subprocess.CompletedProcess(command, 0, stdout="test-token")
        if "login" in command:
            assert kwargs["stdin"] == "test-token"
            return subprocess.CompletedProcess(command, 0)
        calls.append(command)

    monkeypatch.setattr(source, "run", run)

    @contextmanager
    def worktree(*args, **kwargs):
        yield str(tmp_path)

    monkeypatch.setattr(source, "detached_worktree", worktree)
    monkeypatch.setattr(RegistrySession, "manifest_exists", lambda *args: exists)
    server = "localhost:5002" if plain_http else "images.azurecr.io"
    consumer = "registry-test:5000" if plain_http else server
    props = {
        "source_path": "/src/slinky", "source_ref": "feature",
        "destination": {"server": server, "consumer_server": consumer, "plain_http": plain_http},
    }
    if not plain_http:
        props["destination"]["acr_resource_id"] = _ACR_ID
    provider = _ArtifactProvider(
        plan=slinky.artifact_tags, build=slinky.build_and_publish,
        resource_id_repository=slinky.RESOURCE_ID_REPOSITORY,
    )
    result = provider.create(props)
    assert result.id == f"{consumer}/charts/slinky:0.0.0-source1234567890ab"
    assert result.outs["artifact_refs"] == {
        f"charts/{name}": f"{consumer}/charts/{name}:0.0.0-source1234567890ab"
        for name in slinky._CHART_NAMES
    }
    assert result.outs["built"] is not exists
    assert "test-token" not in repr(result.outs)
    if exists:
        assert not calls
    else:
        assert calls[0] == ["make", "VERSION=0.0.0-source1234567890ab", "version-match"]
        assert len([command for command in calls if "package" in command]) == 3
        pushes = [command for command in calls if "push" in command]
        assert len(pushes) == 3
        assert all(f"oci://{server}/charts" in command for command in pushes)
        assert all(("--plain-http" in command) is plain_http for command in pushes)
        assert all(("--registry-config" in command) is not plain_http for command in pushes)
    assert not provider.diff(result.id, result.outs, props).changes
    assert provider.read(result.id, result.outs).id == (result.id if exists else None)


@pytest.mark.parametrize("recipe", ["image", "capz", "slinky"])
def test_resources_resolve_destination_outputs_and_serialize_callbacks(recipe):
    resources = []

    class Mocks(pulumi.runtime.Mocks):
        def new_resource(self, args):
            resources.append(args)
            return args.name, args.inputs

        def call(self, args):
            raise AssertionError(args.token)

    pulumi.runtime.set_mocks(Mocks())

    @pulumi.runtime.test
    def check():
        module = {"image": image, "capz": capz, "slinky": slinky}[recipe]
        resource = PublishedArtifacts(
            "published", plan=module.artifact_tags, build=module.build_and_publish,
            source_path="/src/repo", source_ref="feature",
            build_options={"image_name": pulumi.Output.from_input("custom/image")} if recipe == "image" else None,
            destination={
                "server": pulumi.Output.from_input("localhost:5002"),
                "consumer_server": pulumi.Output.from_input("registry-test:5000"),
                "plain_http": True,
            },
        )

        def verify(urn):
            registered = next(item for item in resources if item.name == "published")
            assert registered.typ == "pulumi-python:dynamic:Resource"
            assert "recipe" not in registered.inputs
            assert "plan" not in registered.inputs
            assert "build" not in registered.inputs
            provider = dill.loads(base64.b64decode(unwrap_rpc_secret(registered.inputs["__provider"])))
            assert provider.plan is module.artifact_tags
            assert provider.build is module.build_and_publish
            if recipe == "image":
                assert registered.inputs["build_options"]["image_name"] == "custom/image"
            assert urn.endswith("::published")
            assert registered.inputs["destination"] == {
                "server": "localhost:5002", "consumer_server": "registry-test:5000", "plain_http": True,
            }

        return resource.urn.apply(verify)

    check()


@pytest.mark.parametrize("scenario,topology", [
    *[(scenario, "local") for scenario in ("omitted", "empty", "docker-image", "capz-bundle", "slinky-charts", "mixed")],
    ("empty", "azure"), ("mixed", "azure"), ("mixed", "hybrid"), ("images-only", "azure"),
])
def test_outer_stack_routes_named_builds(monkeypatch, scenario, topology):
    import stack
    from pko.pko_bootstrap import _init_stack_config_to_config

    resources = []
    captured = {}
    components = {}
    exports = {}
    source_config = {"sourcePath": "/src/repo", "sourceRef": "feature"}
    config = {
        "builds": {
            "capz-controller": {**source_config, "builder": "docker-image", "imageName": "capz/controller"},
            "slurm-operator": {**source_config, "builder": "docker-image", "imageName": "slurm-operator", "target": "custom-stage"},
            "slurm-operator-webhook": {**source_config, "builder": "docker-image", "imageName": "slurm-operator-webhook"},
            "capz-provider": {**source_config, "builder": "capz-bundle"},
            "slinky-charts": {**source_config, "builder": "slinky-charts"},
        },
        "customRegistry": {"name": "build-registry", "port": 5002},
        "initStack": {
            "controlPlane": {"infrastructureProviders": {"azure": {
                "enabled": True,
                "defaultSubscriptionId": "44444444-4444-4444-4444-444444444444",
                "defaultLocation": "westus2",
                "defaultResourceGroup": "host-rg",
                "identity": {
                    "type": "UserAssignedMSI", "clientId": "11111111-1111-1111-1111-111111111111",
                    "tenantId": "33333333-3333-3333-3333-333333333333",
                },
            }}},
            "tenants": {"workloadClusters": {"local": {"className": "local"}}},
        },
    }
    if scenario == "omitted":
        config.pop("builds")
    elif scenario == "empty":
        config["builds"] = {}
    elif scenario == "images-only":
        config["builds"] = {name: build for name, build in config["builds"].items() if name in ("slurm-operator", "slurm-operator-webhook")}
    elif scenario != "mixed":
        config["builds"] = {"standalone": next(build for build in config["builds"].values() if build["builder"] == scenario)}
    if topology != "local":
        clusters = config["initStack"]["tenants"]["workloadClusters"]
        if topology == "azure":
            clusters.clear()
        for name, kind in (("aks", "aks"), ("byo", "azure-byo")):
            clusters[name] = {"className": kind, "parameters": {
                "subscriptionId": "44444444-4444-4444-4444-444444444444",
                "location": "westus2", **({"resourceGroup": "aks-rg"} if kind == "aks" else {}),
            }}
    baseline = stack.InitStackConfig.model_validate(config["initStack"])
    monkeypatch.setattr(stack.pulumi, "Config", lambda: SimpleNamespace(get_object=config.get))
    monkeypatch.setattr(stack.pulumi, "export", lambda name, value: exports.update({name: value}))

    class Mocks(pulumi.runtime.Mocks):
        def new_resource(self, args):
            resources.append(args)
            outputs = dict(args.inputs)
            outputs.setdefault("name", args.name)
            if args.typ == "azure-native:containerregistry:Registry":
                outputs.update(name=args.inputs["registryName"], loginServer="generated.azurecr.io")
            if "destination" in args.inputs:
                provider = dill.loads(base64.b64decode(unwrap_rpc_secret(args.inputs["__provider"])))
                tags = provider.plan(args.inputs.get("build_options") or {}, _COMMIT)
                outputs.update(provider._outputs(args.inputs, _COMMIT, tags))
            resource_id = (
                "/subscriptions/44444444-4444-4444-4444-444444444444/resourceGroups/acr-rg/"
                "providers/Microsoft.ContainerRegistry/registries/generated"
                if args.typ == "azure-native:containerregistry:Registry" else args.name
            )
            return resource_id, outputs

        def call(self, args):
            raise AssertionError(args.token)

    class StubComponent(pulumi.ComponentResource):
        def __init__(self, name, **kwargs):
            super().__init__("test:stack:Component", name)
            components[name] = kwargs
            self.registry_name = pulumi.Output.from_input(kwargs.get("registry_name", name))
            self.port = pulumi.Output.from_input(kwargs.get("port") or 5002)
            self.kubeconfig = pulumi.Output.from_input("{}")
            self.url = pulumi.Output.from_input("oci://registry-service:5000")
            self.service_name = pulumi.Output.from_input("registry-service")
            self.namespace = pulumi.Output.from_input("pulumi-kubernetes-operator")
            self.flux_source = None
            self.webhook_args = {}
            for attribute in (
                "cluster_name", "context", "pid", "log_path", "enable_lb_port_mapping",
                "url_external", "default_branch", "ssh_private_key_secret_name",
                "ssh_private_key_secret_namespace", "service_account", "flux_source_name",
                "flux_source_namespace", "flux_receiver_url", "hook_id", "init_stack",
            ):
                setattr(self, attribute, pulumi.Output.from_input(f"{name}-{attribute}"))

    for symbol in ("CtlptlRegistry", "CtlptlCluster", "CtlptlRegistryService", "CloudProviderKind", "FluxInfrastructure", "GitOpsRepository", "GitOpsWebhook"):
        monkeypatch.setattr(stack, symbol, StubComponent)

    def bootstrap(name, **kwargs):
        captured.update(kwargs)
        return StubComponent(name)

    monkeypatch.setattr(stack, "PKOBootstrap", bootstrap)
    pulumi.runtime.set_mocks(Mocks())

    @pulumi.runtime.test
    def check():
        stack.run_stack()

        def verify(values):
            published = [item for item in resources if "destination" in item.inputs]
            build_configs = config.get("builds", {})
            slurm_images = {"slurm-operator", "slurm-operator-webhook"}
            expected_names = {f"build-{name}" for name in build_configs if topology != "azure" or name not in slurm_images}
            if topology != "local":
                expected_names.update(f"build-{name}-acr" for name in build_configs if name in slurm_images)
            assert {item.name for item in published} == expected_names
            registries = [item for item in resources if item.typ == "azure-native:containerregistry:Registry"]
            assert len(registries) == int(topology != "local")
            if registries:
                assert registries[0].inputs["adminUserEnabled"] is False
                assert registries[0].inputs["sku"] == {"name": "Basic"}
            needs_local_registry = any(topology != "azure" or name not in slurm_images for name in build_configs)
            assert ("custom-registry" in components) == needs_local_registry
            assert ("build_artifact_refs" in exports) == bool(build_configs)
            assert values["exports"]["cluster_name"] == "mgmt-cluster_name"
            assert values["exports"]["cache_registry_name"] == "cache-registry"
            assert values["exports"]["pko_namespace"] == "pulumi-kubernetes-operator"
            assert not {
                "custom_image_refs", "capz_artifact_ref", "capz_artifact_oci_url",
                "slinky_chart_version", "slinky_chart_oci_prefix",
            }.intersection(exports)
            if needs_local_registry:
                assert components["custom-registry"]["registry_name"] == "build-registry"
                assert components["custom-registry"]["port"] == 5002
            for item in published:
                destination = item.inputs["destination"]
                if item.name.endswith("-acr"):
                    assert destination["server"] == destination["consumer_server"] == "generated.azurecr.io"
                    assert destination["plain_http"] is False
                    assert destination["acr_resource_id"].endswith("/registries/generated")
                else:
                    assert destination == {
                        "server": "localhost:5002", "consumer_server": "build-registry:5000", "plain_http": True,
                    }
            assert set(values["refs"]) == set(build_configs)
            for item in published:
                build_config = build_configs[item.name.removeprefix("build-").removesuffix("-acr")]
                expected_builder = stack._BUILDERS[build_config["builder"]]
                provider = dill.loads(base64.b64decode(unwrap_rpc_secret(item.inputs["__provider"])))
                assert provider.plan is expected_builder.artifact_tags
                assert provider.build is expected_builder.build_and_publish
                assert item.inputs["source_path"] == source_config["sourcePath"]
                assert item.inputs["source_ref"] == source_config["sourceRef"]
            resolved = stack.InitStackConfig.model_validate(values["config"])
            local = resolved.tenants.workload_clusters.get("local")
            azure = resolved.control_plane.infrastructure_providers.azure
            if scenario == "mixed":
                operator = next(item for item in published if item.name.startswith("build-slurm-operator") and "webhook" not in item.name)
                assert operator.inputs["build_options"]["target"] == "custom-stage"
                if local is not None:
                    assert local.slinky.operator_image.repository == "build-registry:5000/slurm-operator"
                    assert local.slinky.webhook_image.repository == "build-registry:5000/slurm-operator-webhook"
                    assert local.slinky.operator_chart_version == "0.0.0-source1234567890ab"
                    assert local.slinky.chart_plain_http
                    assert local.custom_registry.registry_name == "build-registry"
                    assert local.custom_registry.port == 5002
                assert azure.controller_image == "build-registry:5000/capz/controller:source-1234567890ab"
                assert azure.provider_oci == "oci://registry-service:5000/capz/cluster-api-provider-azure:source-1234567890ab"
                assert len(values["refs"]["slinky-charts"]["local"]) == 3
            else:
                if local is not None:
                    assert local.slinky == baseline.tenants.workload_clusters["local"].slinky
                    assert local.custom_registry is None
                assert azure.controller_image == baseline.control_plane.infrastructure_providers.azure.controller_image
                assert azure.provider_oci == baseline.control_plane.infrastructure_providers.azure.provider_oci
            if topology != "local":
                for name in ("aks", "byo"):
                    workload = resolved.tenants.workload_clusters[name]
                    assert workload.acr.server == "generated.azurecr.io"
                    if scenario in ("mixed", "images-only"):
                        assert workload.slinky.operator_image.repository == "generated.azurecr.io/slurm-operator"
                        assert workload.slinky.webhook_image.repository == "generated.azurecr.io/slurm-operator-webhook"
                    if scenario == "mixed":
                        assert workload.slinky.chart_plain_http
                        assert workload.slinky.chart_oci_prefix == "oci://registry-service.pulumi-kubernetes-operator.svc.cluster.local:5000/charts"
                        assert workload.slinky.slurm_chart_version == "0.0.0-source1234567890ab"
                    elif scenario != "images-only":
                        assert workload.slinky == baseline.tenants.workload_clusters[name].slinky
                    else:
                        assert workload.slinky.chart_oci_prefix == baseline.tenants.workload_clusters[name].slinky.chart_oci_prefix
            if scenario == "mixed":
                assert set(values["refs"]["slurm-operator"]) == (
                    {"local"} if topology == "local" else {"acr"} if topology == "azure" else {"local", "acr"}
                )

        return pulumi.Output.all(
            config=_init_stack_config_to_config(captured["init_stack_config"]),
            refs=exports.get("build_artifact_refs", {}),
            exports=exports,
        ).apply(verify)

    check()