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

from dataclasses import dataclass
from typing import Any

import pulumi

from stacks.workspace_env import (
    PULUMI_DELETE_UNREACHABLE_ENV,
    PULUMI_DELETE_UNREACHABLE_SECRET_NAME,
)


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

# Volume name used inside the workspace pod for the file:// backend's
# PVC mount. Arbitrary string, just has to match between volume and
# volumeMount entries.
_STATE_VOLUME_NAME = "state"

# Mount path inside the workspace pod. Must agree with the ``file://``
# URL in :mod:`pko._backend`.
_STATE_MOUNT_PATH = "/state"


@dataclass(frozen=True)
class StackCRSpec:
    """Bundle of inputs every Stack CR we emit needs.

    Pure Python-side data carrier. ``PKOBootstrap`` builds one instance and
    serializes its fields into the init Stack CR config; the PKO-owned init
    stack reconstructs it and passes it through to every child Stack-CR build
    path. Callers feed the same :func:`build_stack_spec` to produce the
    ``spec`` dict and then instantiate the ``CustomResource`` themselves. Do
    NOT pass an instance directly as a resource property bag; this is not a
    ``@pulumi.input_type``.

    All Output-typed fields accept either ``Output[str]`` or plain
    ``str``; pulumi-kubernetes resolves Outputs before sending the
    final CR to the API server.
    """

    # Where the Stack CRs themselves live. The PKO operator watches this
    # namespace for Stack CRs.
    pko_namespace: pulumi.Input[str]

    # Workspace pod identity. References the SA created by
    # :class:`pko._service_account.WorkspaceServiceAccount`.
    service_account_name: pulumi.Input[str]

    # Flux Source reference PKO uses to fetch the Pulumi program artifact.
    flux_source_name: pulumi.Input[str]
    flux_source_namespace: pulumi.Input[str]

    # State backend. The PVC is mounted at ``/state`` and the backend
    # URL is ``file:///state``; the passphrase Secret feeds
    # ``PULUMI_CONFIG_PASSPHRASE`` via ``envRefs``.
    state_pvc_name: pulumi.Input[str]
    state_backend_url: pulumi.Input[str]
    passphrase_secret_name: pulumi.Input[str]

    flux_source_api_version: str = "source.toolkit.fluxcd.io/v1"
    flux_source_kind: str = "GitRepository"


def build_stack_spec(
    *,
    spec: StackCRSpec,
    project_name: str,
    env: pulumi.Input[str],
    repo_dir: str,
    config: dict[str, Any] | None = None,
    prerequisites: list[pulumi.Input[str]] | None = None,
) -> dict[str, Any]:
    """Assemble the ``spec`` portion of a ``pulumi.com/v1`` Stack CR.

    Returns the plain dict ready to drop into a
    ``k8s.apiextensions.CustomResource(spec=...)`` call. The caller
    owns the ``CustomResource`` instantiation itself and may mutate
    the returned dict before shipping; nothing here holds a reference.

    Args:
        spec:           Shared shape; see :class:`StackCRSpec`.
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
        A plain ``dict`` suitable as the ``spec=`` argument of
        ``k8s.apiextensions.CustomResource``.
    """
    # Compose spec.stack as a single Output so we don't have to await
    # each component separately.
    stack_name = pulumi.Output.concat(
        f"{_ORG_PLACEHOLDER}/{project_name}/", env
    )

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
        PULUMI_DELETE_UNREACHABLE_ENV: {
            "type": "Secret",
            "secret": {
                "name": PULUMI_DELETE_UNREACHABLE_SECRET_NAME,
                "key": PULUMI_DELETE_UNREACHABLE_ENV,
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
                            "volumeMounts": [
                                {
                                    "name": _STATE_VOLUME_NAME,
                                    "mountPath": _STATE_MOUNT_PATH,
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
                "namespace": spec.flux_source_namespace,
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
