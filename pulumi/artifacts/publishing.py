"""Pulumi lifecycle shared by all source-built artifact recipes."""

from __future__ import annotations

from collections.abc import Callable
import sys

from pulumi import Input, Output, ResourceOptions
from pulumi.dynamic import CreateResult, DiffResult, ReadResult, Resource, ResourceProvider, UpdateResult

from artifacts import source
from artifacts.destination import RegistryDestination, RegistrySession, registry_session


# Plan(build_options, resolved_commit) returns repository -> tag without building.
ArtifactPlan = Callable[[dict, str], dict[str, str]]
# Build(worktree, build_options, planned_tags, session) publishes all planned outputs.
ArtifactBuild = Callable[[str, dict, dict[str, str], RegistrySession], None]


class _ArtifactProvider(ResourceProvider):
    """Run the common Pulumi lifecycle without knowing concrete build recipes.

    The injected callbacks are serialized with the provider; source, destination,
    and build options are resource inputs. Planning must return a deterministic,
    nonempty repository/tag map so refresh can probe outputs without a build.
    Create and update invoke the build callback if any planned artifact is absent.
    """

    def __init__(
        self, *, plan: ArtifactPlan, build: ArtifactBuild,
        resource_id_repository: str | None = None,
    ):
        self.plan = plan
        self.build = build
        self.resource_id_repository = resource_id_repository

    def _outputs(self, props: dict, commit: str, tags: dict[str, str]) -> dict:
        destination = props["destination"]
        return {
            **props,
            "source_commit": commit,
            "artifact_tags": tags,
            "artifact_refs": {
                repository: f"{destination['consumer_server']}/{repository}:{tag}"
                for repository, tag in tags.items()
            },
            "host_artifact_refs": {
                repository: f"{destination['server']}/{repository}:{tag}"
                for repository, tag in tags.items()
            },
        }

    def _ensure(self, props: dict) -> dict:
        with source.source_repository(props) as (repository_path, commit):
            tags = self.plan(props.get("build_options") or {}, commit)
            with registry_session(props["destination"]) as session:
                built = not all(session.manifest_exists(repository, tag) for repository, tag in tags.items())
                if built:
                    # Build and publish share the worktree lifetime; paths never enter state.
                    with source.detached_worktree(repository_path, commit, prefix="ca4s-artifacts-") as worktree:
                        self.build(worktree, props.get("build_options") or {}, tags, session)
            return {**self._outputs(props, commit, tags), "built": built}

    def create(self, props: dict) -> CreateResult:
        outputs = self._ensure(props)
        repository = self.resource_id_repository or next(iter(outputs["artifact_tags"]))
        tag = next(iter(outputs["artifact_tags"].values()))
        resource_id = f"{props['destination']['consumer_server']}/{repository}:{tag}"
        return CreateResult(id_=resource_id, outs=outputs)

    def diff(self, id_: str, olds: dict, news: dict) -> DiffResult:
        # Callback changes live in __provider, not in the ordinary build inputs.
        return DiffResult(changes=source.has_diff(olds, news, (
            "source_path", "repository_url", "source_ref", "destination", "build_options", "__provider",
        )))

    def update(self, id_: str, olds: dict, news: dict) -> UpdateResult:
        return UpdateResult(outs=self._ensure(news))

    def read(self, id_: str, props: dict) -> ReadResult:
        try:
            commit = source.source_commit_for_read(props)
            tags = self.plan(props.get("build_options") or {}, commit)
            with registry_session(props["destination"]) as session:
                if not all(session.manifest_exists(repository, tag) for repository, tag in tags.items()):
                    return ReadResult(id_=None, outs={})
            return ReadResult(id_=id_, outs=self._outputs(props, commit, tags))
        except Exception as exc:
            # An unavailable registry or Git source does not prove the artifacts are gone.
            print(f"failed to refresh published artifacts {id_!r}: {exc}", file=sys.stderr)
            return ReadResult(id_=id_, outs=props)


class PublishedArtifacts(Resource):
    """Publish one or more artifacts from a Git revision using injected functions.

    ``plan(options, commit)`` names the outputs; ``build(worktree, options, tags,
    session)`` builds and publishes them. The build callback may run again after
    partial failure, so it must tolerate artifacts already present in the registry.

    Output maps are keyed by repository, without a registry hostname. Artifact
    references use consumer endpoints; host references use publisher endpoints.
    ``built`` reports whether this operation invoked the build callback.

    ``resource_id_repository`` preserves an existing grouping ID, such as a chart
    set, without adding an artifact to the plan. That ID uses the first planned
    tag; otherwise it uses the first repository/tag pair. Neither callback nor
    temporary build paths are exposed as ordinary resource inputs or outputs.
    """

    source_commit: Output[str]
    artifact_tags: Output[dict[str, str]]
    artifact_refs: Output[dict[str, str]]
    host_artifact_refs: Output[dict[str, str]]
    built: Output[bool]

    def __init__(
        self, name: str, *, plan: ArtifactPlan, build: ArtifactBuild, source_ref: Input[str],
        destination: Input[RegistryDestination], source_path: Input[str] | None = None,
        repository_url: Input[str] | None = None, build_options: Input[dict] | None = None,
        resource_id_repository: str | None = None,
        opts: ResourceOptions | None = None,
    ):
        provider = _ArtifactProvider(plan=plan, build=build, resource_id_repository=resource_id_repository)
        super().__init__(provider, name, {
            "source_path": source_path, "repository_url": repository_url,
            "source_ref": source_ref, "destination": destination, "build_options": build_options,
            "source_commit": None, "artifact_tags": None, "artifact_refs": None,
            "host_artifact_refs": None, "built": None,
        }, opts)

