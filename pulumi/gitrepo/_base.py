"""Provider-agnostic GitOps repository and webhook dispatchers.

This module defines the public dispatching components used by stack entrypoints
and the abstract-ish base shapes concrete implementations satisfy. Concrete
implementations live alongside it (see ``gitea_builtin.py``) and are selected
by provider name.

Why an explicit contract?
-------------------------
Flux source-controller is the downstream consumer that actually reads Git.
Anything *upstream* of that — built-in Gitea, externally-hosted Gitea, GitHub,
GitLab, Bitbucket, your own gitolite — is implementation detail. We model that
contract here so swapping providers is a one-line config change and the rest of
the program (Flux sources, PKO Stacks, seeding scripts, dashboards) doesn't
care which one is in use.

Attributes that every concrete subclass must populate
-----------------------------------------------------
* ``url``               — In-cluster SSH git URL the Flux ``GitRepository``
                          will point at, e.g.
                          ``ssh://git@gitea-ssh.gitea.svc.cluster.local:22/owner/repo.git``.
                          Always SSH for local in-cluster Gitea because it
                          ships no TLS. Cloud providers can still expose HTTPS
                          here.
* ``url_external``      — Host-reachable git URL (typically HTTP/HTTPS) suitable
                          for the one-time hydration ``git push`` we do from
                          the Pulumi runner. May coincide with ``url`` for
                          external providers or be a different scheme (host-
                          side HTTP for in-cluster Gitea).
* ``default_branch``    — Branch name the seed push targeted and the Flux
                          ``GitRepository.spec.ref.branch`` should track.
                          Conventionally ``main``.
* ``ssh_private_key_secret`` — Source Kubernetes Secret resource itself.
                          Its name is usually derived from the matching public
                          key hash so rotations create a new Secret and update
                          downstream references instead of replacing a
                          fixed-name Secret in place.
                          Downstream components use its ``data`` Output to
                          copy the conventional ``id_ed25519`` key into their own namespaces without
                          rereading a just-created Secret from the API server
                          during preview. Callers that need a serializable
                          reference can read ``metadata.name`` and
                          ``metadata.namespace`` from this resource.
* ``ssh_known_hosts``   — Single-line ``known_hosts`` entry for the in-cluster
                          SSH endpoint (``<hostname> <alg> <pubkey-base64>``).
                          Stored with the Flux ``GitRepository`` credentials so
                          source-controller verifies the Gitea host key without
                          manual TOFU. This is public host-key material, not a
                          secret.

Lifetime model
--------------
The public ``GitOpsRepository`` is itself a Pulumi ``ComponentResource`` that
dispatches to a provider-specific child. Tearing down the dispatch component
tears down the concrete git source with it. Good for a local dev loop; in
shared environments you'd point at an external provider that this stack only
references rather than owns.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

import pulumi
from pulumi import Output
from pulumi_kubernetes.core.v1 import Secret


# Single source of truth for the Pulumi resource type token used by the
# base class. Concrete subclasses must override this with their own
# specific token (e.g. ``ca4s:gitrepo:GiteaBuiltinRepository``) so the
# state file faithfully records which implementation produced the
# resource — handy for ``pulumi stack export`` audits and for upgrade
# paths that need to refuse to swap one impl for another.
_BASE_TYPE = "ca4s:gitrepo:GitOpsRepository"
_WEBHOOK_BASE_TYPE = "ca4s:gitrepo:GitOpsWebhook"
_PROVIDER_MODULE_PREFIX = "gitrepo."


def _provider_module_name(provider_name: str) -> str:
    normalized = provider_name.replace("-", "_")
    if not normalized.replace("_", "").isalnum():
        raise ValueError(f"invalid GitOps provider name {provider_name!r}")
    return f"{_PROVIDER_MODULE_PREFIX}{normalized}"


def _provider_class_name(provider_name: str, suffix: str) -> str:
    parts = provider_name.replace("_", "-").split("-")
    return "".join(part.capitalize() for part in parts if part) + suffix


def _load_provider_class(provider_name: str, suffix: str) -> type[Any]:
    module_name = _provider_module_name(provider_name)
    class_name = _provider_class_name(provider_name, suffix)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        raise ValueError(
            f"no GitOps provider {provider_name!r}: expected module "
            f"{module_name!r} exposing class {class_name!r}"
        ) from None
    try:
        return getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(
            f"module {module_name!r} does not expose class {class_name!r}; "
            f"GitOps providers must expose {class_name!r} for provider "
            f"name {provider_name!r}."
        ) from exc


class GitOpsRepositoryProvider(pulumi.ComponentResource):
    """Abstract-ish base for a concrete PKO-consumed git source.

    Subclasses MUST:
      1. Call ``super().__init__(name, t=<their own type token>, opts=opts)``.
         2. Populate every attribute declared in the module docstring. Plain values
            should be wrapped with ``Output.from_input(...)``; resource attributes
            such as ``ssh_private_key_secret`` should hold the resource object.
         3. Call ``self.register_outputs({...})`` with the serializable Output
            values so they show up in ``pulumi stack output`` when the component is
            used at the top level. Resource-object attributes do not need to be
            registered as outputs.

    The base class itself doesn't construct child resources — it's a pure
    contract holder. We deliberately don't make it an ``abc.ABC`` because
    Pulumi's resource machinery introspects the class hierarchy, and adding
    an extra metaclass just to enforce abstractness isn't worth the friction.
    Type checkers will still flag a subclass that forgets to set an output,
    via the attribute declarations below.
    """

    url: "Output[str]"
    url_external: "Output[str]"
    default_branch: "Output[str]"
    ssh_private_key_secret: "Secret"
    ssh_known_hosts: "Output[str]"
    flux_source: pulumi.Resource
    flux_source_name: "Output[str]"
    flux_receiver_token: "Output[str]"
    flux_receiver_url: "Output[str]"
    webhook_args: Mapping[str, Any]

    def __init__(
        self,
        name: str,
        t: str = _BASE_TYPE,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(t, name, props={}, opts=opts)


class GitOpsRepository(pulumi.ComponentResource):
    """Dispatch to a concrete GitOps repository provider by name."""

    url: "Output[str]"
    url_external: "Output[str]"
    default_branch: "Output[str]"
    ssh_private_key_secret: "Secret"
    ssh_known_hosts: "Output[str]"
    flux_source: pulumi.Resource
    flux_source_name: "Output[str]"
    flux_receiver_token: "Output[str]"
    flux_receiver_url: "Output[str]"
    webhook_args: Mapping[str, Any]

    def __init__(
        self,
        name: str,
        *,
        gitops_provider_name: str,
        gitops_provider_args: Mapping[str, Any],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(_BASE_TYPE, name, props={}, opts=opts)

        concrete_cls = _load_provider_class(gitops_provider_name, "Repository")
        concrete = concrete_cls(
            name,
            **dict(gitops_provider_args),
            opts=pulumi.ResourceOptions(parent=self),
        )

        self.url = concrete.url
        self.url_external = concrete.url_external
        self.default_branch = concrete.default_branch
        self.ssh_private_key_secret = concrete.ssh_private_key_secret
        self.ssh_known_hosts = concrete.ssh_known_hosts
        self.flux_source = concrete.flux_source
        self.flux_source_name = concrete.flux_source_name
        self.flux_receiver_token = concrete.flux_receiver_token
        self.flux_receiver_url = concrete.flux_receiver_url
        self.webhook_args = concrete.webhook_args

        self.register_outputs(
            {
                "url": self.url,
                "url_external": self.url_external,
                "default_branch": self.default_branch,
                "ssh_known_hosts": self.ssh_known_hosts,
                "flux_source_name": self.flux_source_name,
                "flux_receiver_token": self.flux_receiver_token,
                "flux_receiver_url": self.flux_receiver_url,
                "webhook_args": self.webhook_args,
            }
        )


class GitOpsWebhookProvider(pulumi.ComponentResource):
    """Abstract-ish base for a concrete GitOps webhook implementation."""

    hook_id: "Output[str]"

    def __init__(
        self,
        name: str,
        t: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(t, name, props={}, opts=opts)


class GitOpsWebhook(pulumi.ComponentResource):
    """Dispatch to a concrete GitOps webhook provider by name."""

    hook_id: "Output[str]"

    def __init__(
        self,
        name: str,
        *,
        gitops_provider_name: str,
        gitops_webhook_args: Mapping[str, Any],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(_WEBHOOK_BASE_TYPE, name, props={}, opts=opts)

        concrete_cls = _load_provider_class(gitops_provider_name, "Webhook")
        concrete = concrete_cls(
            name,
            **dict(gitops_webhook_args),
            opts=pulumi.ResourceOptions(parent=self),
        )
        self.hook_id = concrete.hook_id

        self.register_outputs({"hook_id": self.hook_id})
