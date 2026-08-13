"""Reusable builder for ``pulumi.com/v1`` Stack Custom Resources.

Both :class:`pko.pko_bootstrap.PKOBootstrap` (the init Stack CR) and the
PKO-owned init stack (control-plane plus workload-cluster CRs) emit
Stack CRs with the same boilerplate: ``serviceAccountName``, ``fluxSource``,
``envRefs``, ``backend``, ``workspaceTemplate`` volume mounts, ``refresh:
true``. Factoring that boilerplate here keeps the two callers thin and makes
the CR shape self-documenting in one place.

Source model: Flux owns cloning the in-cluster Gitea repo and producing a source
artifact. PKO consumes that artifact via ``spec.fluxSource`` and no longer needs
per-Stack git credentials or known_hosts mounts.

This module exposes :func:`build_stack_spec` only; callers own the
``k8s.apiextensions.CustomResource`` instantiation itself. Splitting
spec generation from CR creation lets each caller mutate the spec
dict before shipping (extra ``config`` entries, alternate
``resyncFrequencySeconds``, env-scoped ``workspaceTemplate``
overlays, ...) without having to thread every knob through this
builder's signature or grow conditional branches here.

PKO Stack CRD shape (v2) reference:
https://www.pulumi.com/docs/iac/using-pulumi/continuous-delivery/pulumi-kubernetes-operator/stacks/
"""

from __future__ import annotations

from typing import Any

import pulumi

from lib.config import NonEmptyStr, PulumiConfigModel

# Hard-coded org placeholder Pulumi uses for self-managed (file://)
# backends. The full ``spec.stack`` string we emit is
# ``organization/<project>/<env>``; inside the workspace pod
# ``pulumi.get_stack()`` returns just the third segment (``<env>``).
_ORG_PLACEHOLDER = "organization"

# Default container name PKO uses in workspace pods. The
# ``workspaceTemplate.spec.podTemplate`` is a strategic merge patch,
# matching containers by ``name``. If a PKO release changes this name
# this constant is the single place to update.
_WORKSPACE_CONTAINER = "pulumi"

# PKO defaults to the all-language Pulumi image. The init/control-plane/workload
# stacks in this repo are Python-only, so use the smaller pinned Python runtime.
_WORKSPACE_IMAGE = "pulumi/pulumi-python:3.202.0"

# PKO runs workspace pods as UID 1000 without HOME/USER entries in the Python
# image. Point home/cache paths at PKO's writable shared workspace volume, and
# set USER so Pulumi's stack creation path can resolve an identity.
_HOME = "/share"
_PULUMI_HOME = "/share/.pulumi"
_PULUMI_USER = "pulumi"
_PYTHONPATH = "/share/source/pulumi"
_PYTHON_VIRTUALENV_PATH = "/share/source/.venv"
_PYTHON_VIRTUALENV_SUBPATH = "venv"

# Volume name used inside the workspace pod for the file:// backend's
# PVC mount. Arbitrary string, just has to match between volume and
# volumeMount entries.
_STATE_VOLUME_NAME = "state"

# Mount path inside the workspace pod. Must agree with the ``file://``
# URL in :mod:`pko._backend`.
_STATE_MOUNT_PATH = "/state"

_DEFAULT_FLUX_SOURCE_API_VERSION = "source.toolkit.fluxcd.io/v1"
_DEFAULT_FLUX_SOURCE_KIND = "GitRepository"


class StackCRConfig(PulumiConfigModel):
    """Bundle of resolved inputs every Stack CR we emit needs."""

    # Where the Stack CRs themselves live. The PKO operator watches this
    # namespace for Stack CRs.
    pko_namespace: NonEmptyStr

    # Workspace pod identity. References the SA created by
    # :class:`pko._service_account.WorkspaceServiceAccount`.
    service_account_name: NonEmptyStr

    # Flux Source reference PKO uses to fetch the Pulumi program artifact.
    flux_source_name: NonEmptyStr
    flux_source_namespace: NonEmptyStr

    # State backend. The PVC is mounted at ``/state`` and the backend
    # URL is ``file:///state``; the passphrase Secret feeds
    # ``PULUMI_CONFIG_PASSPHRASE`` via ``envRefs``.
    state_pvc_name: NonEmptyStr
    state_backend_url: NonEmptyStr
    passphrase_secret_name: NonEmptyStr
    flux_source_api_version: NonEmptyStr = _DEFAULT_FLUX_SOURCE_API_VERSION
    flux_source_kind: NonEmptyStr = _DEFAULT_FLUX_SOURCE_KIND


def build_stack_spec(
    *,
    spec: pulumi.Input[StackCRConfig],
    project_name: str,
    env: pulumi.Input[str],
    repo_dir: str,
    config: dict[str, Any] | None = None,
    prerequisites: list[pulumi.Input[str]] | None = None,
) -> pulumi.Input[dict[str, Any]]:
    """Assemble the ``spec`` portion of a ``pulumi.com/v1`` Stack CR.

    Returns the dict, or an Output resolving to the dict, ready to drop into a
    ``k8s.apiextensions.CustomResource(spec=...)`` call. The caller
    owns the ``CustomResource`` instantiation itself and may mutate
    the returned dict before shipping when the return value is already plain;
    nothing here holds a reference.

    Args:
        spec:           Shared shape; see :class:`StackCRConfig`.
        project_name:   Kebab-case Pulumi project name (e.g.
                ``ca4s-init``). Becomes the second
                        segment of ``spec.stack``.
        env:            Third segment of ``spec.stack``. For the
                        control-plane CR this is the outer
                        ``pulumi.get_stack()`` value (e.g. ``local``).
                        For workload-cluster CRs this is the outer env; inner
                        ``Tenant<Env>`` components own instance fan-out.
        repo_dir:       Repo-relative path of the inner Pulumi project
                        (e.g. ``pulumi/stacks/control_plane/``).
        config:         Optional inline ``spec.config`` map. Keys are
                        ``<project>:<key>``-style; values may be nested.
        prerequisites:  Optional list of upstream Stack CR names this CR
                        should wait on. Maps to ``spec.prerequisites``.

    Returns:
        A ``dict`` or ``Output[dict]`` suitable as the ``spec=`` argument of
        ``k8s.apiextensions.CustomResource``.
    """
    if not isinstance(spec, StackCRConfig):
        return pulumi.Output.from_input(spec).apply(
            lambda resolved: build_stack_spec(
                spec=StackCRConfig.model_validate(resolved),
                project_name=project_name,
                env=env,
                repo_dir=repo_dir,
                config=config,
                prerequisites=prerequisites,
            )
        )

    # Compose spec.stack as a single Output so we don't have to await
    # each component separately.
    stack_name = pulumi.Output.concat(f"{_ORG_PLACEHOLDER}/{project_name}/", env)

    # PULUMI_CONFIG_PASSPHRASE comes out of the shared passphrase Secret
    # so every inner stack uses the same passphrase against the shared
    # file:// backend (otherwise per-stack passphrases would each need
    # their own salted state subdirectories — workable but more moving
    # parts than we need).
    env_refs = {
        "PULUMI_CONFIG_PASSPHRASE": {
            "type": "Secret",
            "secret": {
                "name": spec.passphrase_secret_name,
                "key": "PULUMI_CONFIG_PASSPHRASE",
            },
        },
        "PULUMI_HOME": {
            "type": "Literal",
            "literal": {
                "value": _PULUMI_HOME,
            },
        },
        "HOME": {
            "type": "Literal",
            "literal": {
                "value": _HOME,
            },
        },
        "USER": {
            "type": "Literal",
            "literal": {
                "value": _PULUMI_USER,
            },
        },
        "PYTHONPATH": {
            "type": "Literal",
            "literal": {
                "value": _PYTHONPATH,
            },
        },
    }

    # Strategic merge patch onto PKO's default workspace pod template. PKO
    # matches containers by ``name``; the default main container is ``pulumi``.
    # The only workspace customization left here is the shared file:// backend
    # PVC mount. Flux handles git authentication and source artifact fetching.
    workspace_template = {
        "spec": {
            "podTemplate": {
                "spec": {
                    "containers": [
                        {
                            "name": _WORKSPACE_CONTAINER,
                            "image": _WORKSPACE_IMAGE,
                            "volumeMounts": [
                                {
                                    "name": _STATE_VOLUME_NAME,
                                    "mountPath": _STATE_MOUNT_PATH,
                                },
                                {
                                    "name": _STATE_VOLUME_NAME,
                                    "mountPath": _PYTHON_VIRTUALENV_PATH,
                                    "subPath": _PYTHON_VIRTUALENV_SUBPATH,
                                },
                            ],
                        },
                    ],
                    "volumes": [
                        {
                            "name": _STATE_VOLUME_NAME,
                            "persistentVolumeClaim": {
                                "claimName": spec.state_pvc_name,
                            },
                        },
                    ],
                },
            },
        },
    }

    cr_spec: dict[str, Any] = {
        "stack": stack_name,
        "fluxSource": {
            "sourceRef": {
                "apiVersion": spec.flux_source_api_version,
                "kind": spec.flux_source_kind,
                "name": spec.flux_source_name,
            },
            "dir": repo_dir,
        },
        "serviceAccountName": spec.service_account_name,
        "envRefs": env_refs,
        "backend": spec.state_backend_url,
        "workspaceTemplate": workspace_template,
        # refresh before every up so detected drift converges. The PKO
        # docs explicitly recommend this for non-Pulumi-Cloud backends.
        "refresh": True,
        # Resync ON commit match so periodic drift detection runs even
        # when the source repo hasn't moved. 1 hour cadence is fine for
        # local dev; tune per env if/when needed.
        "continueResyncOnCommitMatch": True,
        "resyncFrequencySeconds": 3600,
        # On Stack CR delete, run ``pulumi destroy`` first to tear down
        # what the inner stack created. Without this, deleting the CR
        # leaks the inner resources.
        "destroyOnFinalize": True,
    }

    if config:
        cr_spec["config"] = config
    if prerequisites:
        cr_spec["prerequisites"] = [
            {"name": p} for p in prerequisites
        ]

    return cr_spec
