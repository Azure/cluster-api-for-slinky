"""Reusable builder for ``pulumi.com/v1`` Stack Custom Resources.

Both :class:`pko.pko_bootstrap.PKOBootstrap` (control-plane CR) and
:class:`pko._tenants_local.TenantsLocal` (per-tenant workload-cluster
CRs) emit Stack CRs with the same boilerplate: ``serviceAccountName``,
``projectRepo`` + ``branch`` + ``gitAuth.sshAuth``, ``envRefs``,
``backend``, ``workspaceTemplate`` volume mounts, ``refresh: true``.
Factoring that boilerplate here keeps the two callers thin and makes
the CR shape self-documenting in one place.

Source model: SSH against the in-cluster Gitea. PKO's ``projectRepo``
path only accepts ``https`` and ``ssh`` schemes (the ``gitutil``
allow-list rejects plain HTTP outright), and our in-cluster Gitea
ships no TLS — SSH is the only viable scheme. The admin private key
is projected into PKO's namespace as a Secret named in
``StackCRSpec.ssh_secret_name``; this builder references it via
``gitAuth.sshAuth.sshPrivateKey``. The public Gitea host key is
projected separately as a known_hosts ConfigMap and mounted into
workspace pods so go-git's SSH host-key verification passes.

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

# Default init container name PKO injects to do the ``git clone`` of
# the inner project repo. The strategic merge by ``name`` targets it
# so we can mount known_hosts + set ``SSH_KNOWN_HOSTS`` there too;
# without it the clone runs without host-key trust and fails with
# "unable to find any valid known_hosts file" before the main
# ``pulumi`` container ever starts.
_WORKSPACE_FETCH_CONTAINER = "fetch"

# Volume name used inside the workspace pod for the file:// backend's
# PVC mount. Arbitrary string, just has to match between volume and
# volumeMount entries.
_STATE_VOLUME_NAME = "state"

# Mount path inside the workspace pod. Must agree with the ``file://``
# URL in :mod:`pko._backend`.
_STATE_MOUNT_PATH = "/state"

# Volume name + mount path for the in-cluster Gitea known_hosts ConfigMap.
# Mirrors the PKO operator pod's own mount (see :mod:`pko._release`) so the
# workspace pod's ``git clone`` step trusts the same host key by the same env
# var.
_KNOWN_HOSTS_VOLUME_NAME = "gitea-known-hosts"
_SSH_MOUNT_PATH = "/etc/gitea-ssh"

@dataclass(frozen=True)
class StackCRSpec:
    """Bundle of inputs every Stack CR we emit needs.

    Pure Python-side data carrier — ``PKOBootstrap`` builds one
    instance, holds it as a Python attribute, and passes it through to
    every Stack-CR build path: the control-plane CR directly in
    :class:`pko.pko_bootstrap.PKOBootstrap`; the per-tenant
    workload-cluster CRs via :class:`pko._tenants.Tenants` -> the
    per-env concrete impl. Both call sites feed the same
    :func:`build_stack_spec` to produce the ``spec`` dict and then
    instantiate the ``CustomResource`` themselves. Never crosses the
    Pulumi RPC boundary itself — callers always read its fields out
    into the plain dicts that pulumi-kubernetes consumes. Do NOT pass
    an instance directly as a resource property bag; this is not a
    ``@pulumi.input_type``.

    All Output-typed fields accept either ``Output[str]`` or plain
    ``str``; pulumi-kubernetes resolves Outputs before sending the
    final CR to the API server.
    """

    # Where the Stack CRs themselves live. The PKO operator watches
    # this namespace for Stack CRs; same ns as the workspace SA and
    # the shared Flux GitRepository CR.
    pko_namespace: pulumi.Input[str]

    # Workspace pod identity. References the SA created by
    # :class:`pko._service_account.WorkspaceServiceAccount`.
    service_account_name: pulumi.Input[str]

    # Git source. SSH URL (``ssh://git@...``) + branch the inner stack
    # tracks. The branch is conventionally ``main`` — see
    # :mod:`gitrepo._base`.
    repo_url: pulumi.Input[str]
    repo_branch: pulumi.Input[str]

    # SSH Secret in ``pko_namespace`` exposing the admin private key.
    ssh_secret_name: pulumi.Input[str]

    # ConfigMap in ``pko_namespace`` exposing the known_hosts entry for
    # go-git's SSH host-key verification.
    known_hosts_config_map_name: pulumi.Input[str]

    # State backend. The PVC is mounted at ``/state`` and the backend
    # URL is ``file:///state``; the passphrase Secret feeds
    # ``PULUMI_CONFIG_PASSPHRASE`` via ``envRefs``.
    state_pvc_name: pulumi.Input[str]
    state_backend_url: pulumi.Input[str]
    passphrase_secret_name: pulumi.Input[str]

    # Key names inside the Secret/ConfigMap. Defaulted because every caller
    # uses the canonical names; kept as fields so a future cloud-hosted
    # gitops provider can override without touching the Stack CR code.
    ssh_private_key_key: str = "id_ed25519"
    ssh_known_hosts_key: str = "known_hosts"


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
                        ``ca4s-control-plane`` or
                        ``ca4s-workload-cluster``). Becomes the second
                        segment of ``spec.stack``.
        env:            Third segment of ``spec.stack``. For the
                        control-plane CR this is the outer
                        ``pulumi.get_stack()`` value (e.g. ``local``).
                        For per-tenant workload-cluster CRs this is
                        ``<outer_env>-<tenant>``.
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

    # ``gitAuth.sshAuth.sshPrivateKey`` points the PKO controller at
    # the ``id_ed25519`` key inside our SSH Secret. PKO's go-git client
    # signs the connection with this key. NB: previously we routed through
    # Flux to dodge PKO's HTTP-scheme rejection; now that we speak SSH
    # directly, ``projectRepo`` is back on the menu and Flux is gone.
    git_auth = {
        "sshAuth": {
            "sshPrivateKey": {
                "type": "Secret",
                "secret": {
                    "name": spec.ssh_secret_name,
                    "key": spec.ssh_private_key_key,
                },
            },
        },
    }

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
    }

    # Strategic merge patch onto PKO's default workspace pod template.
    # PKO matches containers by ``name``; the default main container is
    # ``pulumi`` (constant above) and the default init container that
    # runs the ``git clone`` is ``fetch``. Both need the known_hosts
    # mount + ``SSH_KNOWN_HOSTS`` env: ``fetch`` so go-git inside
    # PKO's clone init can verify the Gitea host key, and ``pulumi``
    # so the auto-api inside the workspace can re-resolve the same
    # remote if the inner program ever does its own git ops. The main
    # container also gets the state PVC mount; the init container
    # doesn't (it writes only into the shared ``/share`` emptyDir
    # that PKO injects automatically).
    ssh_env = [
        {
            "name": "SSH_KNOWN_HOSTS",
            "value": (
                f"{_SSH_MOUNT_PATH}/{spec.ssh_known_hosts_key}"
            ),
        },
    ]
    ssh_volume_mount = {
        "name": _KNOWN_HOSTS_VOLUME_NAME,
        "mountPath": _SSH_MOUNT_PATH,
        "readOnly": True,
    }
    workspace_template = {
        "spec": {
            "podTemplate": {
                "spec": {
                    "initContainers": [
                        {
                            "name": _WORKSPACE_FETCH_CONTAINER,
                            "env": ssh_env,
                            "volumeMounts": [ssh_volume_mount],
                        },
                    ],
                    "containers": [
                        {
                            "name": _WORKSPACE_CONTAINER,
                            "env": ssh_env,
                            "volumeMounts": [
                                {
                                    "name": _STATE_VOLUME_NAME,
                                    "mountPath": _STATE_MOUNT_PATH,
                                },
                                ssh_volume_mount,
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
                        {
                            "name": _KNOWN_HOSTS_VOLUME_NAME,
                            "configMap": {
                                "name": spec.known_hosts_config_map_name,
                                "items": [
                                    {
                                        "key": spec.ssh_known_hosts_key,
                                        "path": spec.ssh_known_hosts_key,
                                    },
                                ],
                                "defaultMode": 0o444,
                            },
                        },
                    ],
                },
            },
        },
    }

    cr_spec: dict[str, Any] = {
        "stack": stack_name,
        "projectRepo": spec.repo_url,
        "repoDir": repo_dir,
        "branch": spec.repo_branch,
        "gitAuth": git_auth,
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
