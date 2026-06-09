"""Built-in Gitea ``GitOpsRepository`` implementation.

This module stands up a self-contained, ephemeral Gitea instance inside the
management cluster and exposes it through the
:class:`~gitrepo._base.GitOpsRepositoryProvider` contract so downstream Flux/PKO
resources can consume it without knowing how the git server was created.

What we deploy
--------------
* A single namespace, ``gitea``, holding the server and the credentials
  Secret.
* A ``RandomPassword`` for the admin user. Held in Pulumi state and never
  surfaced as a stack output.
* One credentials ``Secret`` in the ``gitea`` namespace named
    ``gitea-credentials`` with two keys: ``username`` and ``password``.
    The Gitea Helm chart reads it via ``gitea.admin.existingSecret`` to
    bootstrap the admin user on first boot. (``email`` is sourced from
    chart values directly, not from the Secret, so it isn't a Secret key.)

    Flux source-controller reads the repo through an uploaded admin SSH public
    key, not this HTTP admin password.
* A ``helm.v3.Release`` of ``gitea-charts/gitea`` (pinned version below)
  configured for the minimal footprint: no postgres, no redis, no
  external cache, sqlite3 DB, memory session/cache, level queue. A
    small (2 GiB) PVC backs ``/data`` via kind's local-path provisioner
    so the synced repo + admin DB survive pod restarts; the chart's
  ``helm.sh/resource-policy: keep`` annotation is explicitly stripped
  so ``pulumi destroy`` / ``helm uninstall`` reclaims the PVC rather
  than leaking it. The HTTP Service is exposed as ``LoadBalancer`` so
    cloud-provider-kind can publish a host-reachable address for the bridged
    Gitea provider and ``GiteaSync`` to use; SSH stays on ``ClusterIP``.
* A ``pulumi_gitea.Repository`` that lands an empty
    ``<owner>/<repo_name>`` inside Gitea.
* A ``GiteaSync`` (``gitrepo.gitea_sync``) that pushes the local working
    tree's current ``HEAD`` into the repo's default branch when the remote
    branch is missing or stale. It never force-pushes and resolves the
    current LoadBalancer address right before talking to Git.

What we don't deploy (yet)
--------------------------
* No Ingress / TLS. The HTTP Service rides on cloud-provider-kind's
  per-cluster-bridge LB IP (e.g. ``172.18.0.x:3000``) — host-reachable
  but not exposed off-host. A future PKO Stack can drop in
  ingress-nginx + a real cert if/when needed.
* No dedicated read-only user. The sync pushes as the admin user and
  ``gitea-credentials`` reuses the admin password. If multi-tenancy
  starts to matter, introduce a ``gitea-pko`` user via the REST API
  and point the credentials Secret at that instead.

Pin policy
----------
Chart version is pinned (see ``_GITEA_CHART_VERSION``). Upgrades are an
explicit edit + ``pulumi up``, not an implicit "always-latest" drift —
matching the rest of this stack's conservative defaults.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any, Mapping, Optional

import pulumi
import pulumi_gitea as gitea_sdk
import pulumi_kubernetes as k8s
import pulumi_random as random
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from pulumi import Output, ResourceOptions

from gitrepo._base import GitOpsRepositoryProvider, GitOpsWebhookProvider
from gitrepo.gitea_sync import GiteaSync
from pko._flux import FluxGitSource


# Pinned upstream chart. Bump this together with ``_GITEA_APP_VERSION``
# (which is informational only — surfaced for log/output use, not passed
# to the chart) when refreshing. Chart 12.6.0 ships Gitea 1.26.1.
_GITEA_CHART_REPO = "https://dl.gitea.com/charts/"
_GITEA_CHART_NAME = "gitea"
_GITEA_CHART_VERSION = "12.6.0"
_GITEA_APP_VERSION = "1.26.1"  # informational

# Namespace this component owns. We don't make it configurable — there's
# no real use case for renaming it and hard-coding keeps the consumer
# side simpler. We deliberately don't pre-create a PKO-specific namespace
# here; whoever installs PKO owns that decision.
_GITEA_NAMESPACE = "gitea"

# The credentials Secret name. The Gitea Helm chart reads it via
# ``gitea.admin.existingSecret`` (pulling ``username`` / ``password`` keys
# out of it to bootstrap the admin user on first boot). Flux uses SSH auth for
# Git reads and never consumes this Secret directly.
_CREDENTIALS_SECRET = "gitea-credentials"

# Gitea refuses the literal string ``admin`` as an administrator username
# (it's reserved). We pick a sane default and let the user override via
# config without ever colliding with that reservation.
_DEFAULT_ADMIN_USERNAME = "caps-admin"
_DEFAULT_ADMIN_EMAIL = "caps-admin@example.invalid"

# Repo coordinates the sync phase will use. ``_DEFAULT_REPO_OWNER`` is the
# Gitea org / user that will own the synced repo; defaulting it to the admin
# username keeps the URL self-describing.
_DEFAULT_REPO_NAME = "cluster-api-provider-slinky"
_DEFAULT_BRANCH = "main"

# In-cluster service coordinates. The upstream Gitea chart names its HTTP
# Service ``gitea-http`` and exposes port 3000 by default; we don't override
# either, so these are the values to use in the in-cluster URL.
_GITEA_HTTP_SERVICE = "gitea-http"
_GITEA_HTTP_PORT = 3000

# SSH endpoint. The chart's default ``service.ssh`` is a *headless*
# ClusterIP (``clusterIP: None``), which we explicitly override below
# to a regular ClusterIP — a regular vIP gives Flux source-controller
# a stable target that resolves to the Gitea pod's current IP
# without relying on the headless-Service DNS quirk that returns the
# pod IP directly (and would change every restart, busting known_hosts
# entries pinned by IP if we ever did that).
_GITEA_SSH_SERVICE = "gitea-ssh"
_GITEA_SSH_PORT = 22

# Prefix for the Secret holding the pre-generated Gitea SSH *host* keypair. Mounted
# into the Gitea pod via the chart's ``extraVolumes`` +
# ``extraContainerVolumeMounts`` so Gitea boots with a deterministic
# host key (instead of auto-generating one on first boot, which would
# bust our pre-computed ``known_hosts`` entry). Two keys:
#   * ``ssh_host_ed25519_key``      — PKCS8 PEM private key.
#   * ``ssh_host_ed25519_key.pub``  — OpenSSH-format public key.
_HOST_KEY_SECRET_PREFIX = "gitea-ssh-host-keys-pkcs8"

# Prefix for the source Secret that holds the admin user's SSH private key. The
# name is derived from the matching public key so every consumer follows the
# same key-version identity.
_USER_KEY_SECRET_PREFIX = "gitea-ssh"
_USER_PRIVATE_KEY_SECRET_KEY = "id_ed25519"

# Title we give the admin user's SSH key inside Gitea. Surfaces in the
# Gitea web UI under the admin user's SSH keys list — useful when
# eyeballing what Pulumi actually wired up.
_ADMIN_SSH_KEY_TITLE = "ca4s-pko"

# Default location of the local working tree the sync pushes from is
# resolved lazily by :func:`_default_source_dir` — see the docstring there
# for why this is not a module-level constant.


def _default_source_dir() -> str:
    """Resolve the repo root as a path relative to the working directory.

    Uses the documented :func:`pulumi.get_root_directory` API to discover
    the dir holding ``Pulumi.yaml`` (the Pulumi project root), then walks
    up one level to the enclosing repo root. The result is converted to a
    CWD-relative path so the stored resource input does not shift between
    machines with different absolute repo roots — see
    https://www.pulumi.com/docs/iac/concepts/projects/#root-relative-paths

    Evaluated lazily (rather than at module import) because Pulumi's
    dynamic-Resource subprocesses import this module *without* the runtime
    settings initialized, in which case ``get_root_directory()`` returns a
    placeholder string. Deferring to call time means only the language
    host — where settings *are* populated — ever runs this.
    """
    return os.path.relpath(
        os.path.dirname(pulumi.get_root_directory()),
        start=os.getcwd(),
    )


def _chart_values(
    admin_secret_name: str,
    admin_email: str,
    host_key_secret_name: pulumi.Input[str],
) -> Mapping[str, Any]:
    """Return the Helm values dict for the pinned Gitea chart.

    Kept as a module-level function (rather than inlined in ``__init__``) so
    the policy choices are easy to diff in isolation when the chart version
    moves and the values schema shifts under us.

    ``admin_email`` is passed in (rather than read from the Secret) because
    the chart's init script sources ``email`` from
    ``.Values.gitea.admin.email`` directly — only ``username`` and
    ``password`` are wired from ``existingSecret``.

    ``host_key_secret_name`` is the name of the pre-generated Gitea SSH
    host keypair Secret (single ed25519 pair) we inject into the pod via
    ``extraVolumes`` + ``extraContainerVolumeMounts`` subPath-overrides on
    ``/data/ssh/ssh_host_ed25519_key{,.pub}``. Restricting the offered
    host-key set to ed25519-only via ``server.SSH_SERVER_HOST_KEYS``
    means the operator pod's ``known_hosts`` only needs one line.
    """
    return {
        # Force the chart's ``fullname`` template to produce the static
        # string ``gitea`` instead of the default ``<release-name>-<chart-
        # name>`` combo. The chart applies this to every resource it
        # generates, so all the Service / ConfigMap / Secret names become
        # stable and predictable. Crucial because we hard-code the
        # in-cluster URL (``gitea-http.gitea.svc.cluster.local``) into the
        # GitOpsRepository ``url`` output — if the service name drifted
        # with each Pulumi release, every PKO Stack downstream would have
        # to re-resolve it.
        "fullnameOverride": "gitea",
        # No external datastore: sqlite + in-process queue + memory cache.
        # State lands on a small PVC mounted at ``/data`` (see below) so
        # pod restarts don't wipe the seeded repo.
        #
        # Chart v12 renamed the Redis subcharts to Valkey ("valkey-cluster"
        # is the new default, on by default!). The legacy ``redis`` /
        # ``redis-cluster`` keys are silently ignored, which is exactly the
        # kind of "my override did nothing" bug that wastes the most time.
        # ``valkey-cluster.enabled=false`` is the one that matters — without
        # it, the chart auto-wires Gitea's cache + queue to point at a
        # 3-replica valkey StatefulSet, and any ``gitea.config.cache.*``
        # overrides get clobbered.
        "valkey-cluster": {"enabled": False},
        "valkey": {"enabled": False},
        "postgresql": {"enabled": False},
        "postgresql-ha": {"enabled": False},
        # Single-replica Gitea backed by a small PVC. 2 GiB is comically
        # generous for one small repo; the chart default of 10 GiB is
        # straight-up wasteful for an ephemeral dev instance.
        #
        # ``annotations.helm.sh/resource-policy: None`` is the important
        # bit: the chart defaults to keeping the PVC after uninstall,
        # but this is a disposable local git server and ``pulumi destroy``
        # should clean up the repo data too.
        "persistence": {
            "enabled": True,
            "size": "2Gi",
            "annotations": {
                "helm.sh/resource-policy": None,
            },
        },
        # Keep exactly one Gitea pod touching the PVC-backed sqlite DB and
        # level queue. With the chart's default rolling strategy, Kubernetes
        # can briefly run old+new pods at once and Gitea fails on the queue
        # lock. This is independent of Valkey, which is disabled above.
        "strategy": {
            "type": "Recreate",
        },
        # HTTP exposed as ``LoadBalancer`` so cloud-provider-kind can
        # publish it on a host-reachable address. We need that address
        # at two points in this stack:
        #   * pulumi_gitea.Repository, to call the Gitea REST admin API
        #     from the host;
        #   * GiteaSync, to ``git push`` the local working tree.
        # SSH stays ``ClusterIP`` — host-side sync reaches it through a
        # short-lived kubectl port-forward, avoiding a LAN-visible SSH
        # endpoint.
        #
        # ``clusterIP: ""`` is critical here: the chart defaults this to
        # ``None`` (i.e. a *headless* Service), which is incompatible
        # with type=LoadBalancer (k8s rejects with
        # ``spec.clusterIPs[0]: Invalid value: 'None'``). Setting it to
        # the empty string asks k8s to auto-assign a virtual IP, which
        # is what every "normal" Service does.
        "service": {
            "http": {
                "type": "LoadBalancer",
                "clusterIP": "",
            },
            # SSH stays in-cluster (we never expose it to the host).
            # Override the chart's headless default (``clusterIP: None``)
            # with the empty string so k8s assigns a regular vIP — the
            # Flux source-controller dials this Service by DNS, and a stable vIP is
            # the friendlier failure mode for debugging.
            "ssh": {
                "type": "ClusterIP",
                "clusterIP": "",
            },
        },
        # Mount the pre-generated host keypair Secret onto the Gitea
        # data dir. ``subPath`` overlays a single file from the Secret
        # on top of the PVC at the named path — the rest of
        # ``/data/ssh/`` remains PVC-backed, which is what Gitea
        # expects (it writes ``authorized_keys`` there at runtime).
        # We don't ship rsa / ecdsa host keys; ``server.SSH_SERVER_HOST_KEYS``
        # below restricts Gitea to ed25519-only, matching the single
        # ``known_hosts`` entry the operator pod will trust.
        "extraVolumes": [
            {
                "name": "gitea-ssh-host-keys",
                "secret": {
                    "secretName": host_key_secret_name,
                    "defaultMode": 0o600,
                },
            },
        ],
        "extraContainerVolumeMounts": [
            {
                "name": "gitea-ssh-host-keys",
                "mountPath": "/data/ssh/ssh_host_ed25519_key",
                "subPath": "ssh_host_ed25519_key",
                "readOnly": True,
            },
            {
                "name": "gitea-ssh-host-keys",
                "mountPath": "/data/ssh/ssh_host_ed25519_key.pub",
                "subPath": "ssh_host_ed25519_key.pub",
                "readOnly": True,
            },
        ],
        "gitea": {
            # Admin user is bootstrapped on first boot. ``existingSecret``
            # supplies ``username`` / ``password`` via ``secretKeyRef`` on
            # the chart's init container; ``email`` is read straight out
            # of these values by the chart's init script, so we pass it
            # inline rather than burying it in the Secret where the chart
            # would ignore it anyway.
            "admin": {
                "existingSecret": admin_secret_name,
                "email": admin_email,
            },
            "config": {
                "database": {"DB_TYPE": "sqlite3"},
                "session": {"PROVIDER": "memory"},
                "cache": {"ADAPTER": "memory"},
                "queue": {"TYPE": "level"},
                # Pin host-key algorithm set to ed25519-only. Gitea
                # default offers all three (rsa, ecdsa, ed25519) and
                # auto-generates whichever ones are missing on first
                # boot — if we let it generate rsa+ecdsa, the SSH
                # negotiation might pick one of those and the operator's
                # ed25519-only known_hosts entry would fail to match.
                "server": {
                    "SSH_SERVER_HOST_KEYS": "ssh/ssh_host_ed25519_key",
                },
            },
        },
    }


def _in_cluster_ssh_url(owner: str, repo: str) -> str:
    """Compute the in-cluster git SSH URL for ``owner/repo``.

    Flux supports SSH URLs and expects this full URL shape rather than scp-like
    shorthand. Service name + namespace + port are pinned by the chart values
    above; this is a pure formatting helper.
    """
    return (
        f"ssh://git@{_GITEA_SSH_SERVICE}.{_GITEA_NAMESPACE}.svc.cluster.local"
        f":{_GITEA_SSH_PORT}/{owner}/{repo}.git"
    )


def _in_cluster_ssh_host() -> str:
    """Hostname that appears in the SSH URL and the ``known_hosts`` entry."""
    return f"{_GITEA_SSH_SERVICE}.{_GITEA_NAMESPACE}.svc.cluster.local"


def _short_public_key_hash(public_key: str) -> str:
    """Return a short stable hash for an OpenSSH public key string."""
    parts = public_key.strip().split()
    if len(parts) < 2:
        raise ValueError(f"expected OpenSSH public key, got {public_key!r}")
    normalized = " ".join(parts[:2])
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()[:12]


def _name_with_public_key_hash(prefix: str, public_key: str) -> str:
    """Build a Kubernetes-safe name that changes only when the public key does."""
    return f"{prefix}-{_short_public_key_hash(public_key)}"


def _k8s_secret_data(value: str) -> str:
    return base64.b64encode(value.encode("ascii")).decode("ascii")


def _derive_ed25519_keypair(seed_b64: str) -> dict[str, str]:
    """Derive an ed25519 keypair from 32 bytes of randomness.

    Pulumi state already persists the seed bytes (via
    :class:`pulumi_random.RandomBytes`), so derivation is deterministic
    across reapplies — changing the seed bytes is the only thing that
    rotates the key. Returns PKCS8 PEM private and OpenSSH public
    (single-line ``ssh-ed25519 <base64> <comment>``-style without the
    comment) strings.
    """
    seed = base64.b64decode(seed_b64)
    if len(seed) != 32:
        raise ValueError(
            f"ed25519 seed must be 32 bytes, got {len(seed)} (b64='{seed_b64[:20]}...')"
        )
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    pub = priv.public_key()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    pub_openssh = pub.public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    return {"private": priv_pem, "public": pub_openssh}


class GiteaBuiltinRepository(GitOpsRepositoryProvider):
    """In-cluster, ephemeral Gitea backing the GitOps source.

    Parameters
    ----------
    name :
        Pulumi resource name.
    kubeconfig :
        Kubeconfig contents (string). Plumbed through to a dedicated
        ``kubernetes.Provider`` so this stack doesn't accidentally use the
        ambient ``~/.kube/config`` context, which can drift if the user
        ``kubectl config use-context`` to something else mid-session.
    flux_provider, flux_infrastructure, pko_namespace_resource :
        Management-cluster handles for declaring the Flux ``GitRepository``
        source that PKO Stack CRs consume. The Gitea implementation owns this
        because it knows the provider-specific SSH Secret and known_hosts shape.
    admin_username, admin_email :
        Override the defaults for the Gitea admin user. ``admin_username``
        must not be the literal ``"admin"`` (Gitea reserves it).
    repo_owner, repo_name, default_branch :
        Coordinates for the Gitea repository and ``GiteaSync`` children:
        which Gitea user/org owns the seeded repo, what it's called,
        and what branch the local working tree gets pushed to.
        ``repo_owner`` defaults to ``admin_username`` so the URL is
        self-describing. Surfaced verbatim into the ``url`` /
        ``url_external`` outputs.
    source_dir :
        Local git working tree the sync pushes from. Defaults to
        the repo root that contains this Pulumi project. ``HEAD`` of
        this directory is resolved at construction time and captured
        as a sync input — when it advances, the sync pushes. ``sync_triggers``
        can force the same push path without changing ``HEAD``.
    sync_triggers :
        Optional operator-controlled replacement inputs for ``GiteaSync``.
        Changing any key/value forces a non-force push attempt.
    opts :
        Standard Pulumi ``ResourceOptions``.
    """

    def __init__(
        self,
        name: str,
        kubeconfig: Output[str] | str,
        *,
        flux_provider: k8s.Provider,
        flux_infrastructure: pulumi.Resource,
        pko_namespace_resource: pulumi.Resource,
        admin_username: str = _DEFAULT_ADMIN_USERNAME,
        admin_email: str = _DEFAULT_ADMIN_EMAIL,
        repo_owner: Optional[str] = None,
        repo_name: str = _DEFAULT_REPO_NAME,
        default_branch: str = _DEFAULT_BRANCH,
        source_dir: Optional[str] = None,
        sync_triggers: Optional[dict[str, Any]] = None,
        opts: Optional[ResourceOptions] = None,
    ) -> None:
        super().__init__(
            name,
            t="ca4s:gitrepo:GiteaBuiltinRepository",
            opts=opts,
        )

        if source_dir is None:
            source_dir = _default_source_dir()

        if admin_username.lower() == "admin":
            # Fail loud — Gitea will reject this on first boot and the
            # error message there is opaque enough to waste an afternoon.
            raise ValueError(
                "admin_username='admin' is reserved by Gitea; pick something else "
                "(default is 'caps-admin')."
            )
        owner = repo_owner if repo_owner is not None else admin_username

        # Dedicated Kubernetes provider parented to this component. Every
        # child k8s resource carries it in its ResourceOptions so they all
        # speak to the same cluster regardless of host kubectl context.
        k8s_provider = k8s.Provider(
            f"{name}-k8s",
            kubeconfig=kubeconfig,
            opts=ResourceOptions(parent=self),
        )
        k8s_opts = ResourceOptions(parent=self, provider=k8s_provider)

        # The one namespace this component owns. Both the server and
        # the credentials Secret live here.
        gitea_ns = k8s.core.v1.Namespace(
            f"{name}-gitea-ns",
            metadata={"name": _GITEA_NAMESPACE},
            opts=k8s_opts,
        )

        # Strong, alphanumeric-only admin password. We avoid special chars
        # so the password survives every shell / URL / env-var round-trip
        # we might subject it to during seeding without quoting bugs.
        admin_password = random.RandomPassword(
            f"{name}-admin-password",
            length=32,
            special=False,
            opts=ResourceOptions(parent=self),
        )

        # The Gitea Helm chart reads this via ``gitea.admin.existingSecret``
        # on first boot and runs ``gitea admin user create``. Order matters:
        # it must exist before the Helm release reaches its admin-init job.
        # Type ``Opaque`` keeps the exact chart-expected keys explicit.
        credentials_secret = k8s.core.v1.Secret(
            f"{name}-credentials",
            metadata={
                "name": _CREDENTIALS_SECRET,
                "namespace": _GITEA_NAMESPACE,
            },
            type="Opaque",
            immutable=True,
            string_data={
                "username": admin_username,
                "password": admin_password.result,
            },
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[gitea_ns],
            ),
        )

        # ----------------------------------------------------------------
        # SSH keypair generation (host key for Gitea + admin user key).
        # ----------------------------------------------------------------
        #
        # Two ed25519 keypairs, both derived deterministically from
        # ``RandomBytes`` seeds. ``RandomBytes`` persists the bytes in
        # Pulumi state (encrypted by the stack's passphrase), so reapplies
        # produce the same keys until you explicitly rotate by tainting
        # the RandomBytes resource. Deriving via cryptography in an
        # ``Output.apply`` keeps the private key bytes inside the
        # language host \u2014 they never leak to the wire as a separate
        # Pulumi resource property.
        host_seed = random.RandomBytes(
            f"{name}-ssh-host-seed",
            length=32,
            opts=ResourceOptions(parent=self),
        )
        user_seed = random.RandomBytes(
            f"{name}-ssh-user-seed",
            length=32,
            opts=ResourceOptions(parent=self),
        )
        host_keypair = host_seed.base64.apply(_derive_ed25519_keypair)
        user_keypair = user_seed.base64.apply(_derive_ed25519_keypair)

        host_private_pem: Output[str] = host_keypair["private"]
        host_public_openssh: Output[str] = Output.unsecret(host_keypair["public"])
        user_private_pem: Output[str] = user_keypair["private"]
        user_public_openssh: Output[str] = Output.unsecret(user_keypair["public"])

        host_key_secret_name = host_public_openssh.apply(
            lambda pub: _name_with_public_key_hash(_HOST_KEY_SECRET_PREFIX, pub)
        )
        user_key_secret_name = user_public_openssh.apply(
            lambda pub: _name_with_public_key_hash(_USER_KEY_SECRET_PREFIX, pub)
        )

        # Secret holding the Gitea SSH host keypair. Mounted into the
        # Gitea pod by ``_chart_values`` so the first-boot host-key
        # auto-generation step is skipped \u2014 our pre-computed
        # ``known_hosts`` line will match the key Gitea presents on
        # the wire.
        host_key_secret = k8s.core.v1.Secret(
            f"{name}-ssh-host-keys",
            metadata={
                "name": host_key_secret_name,
                "namespace": _GITEA_NAMESPACE,
            },
            type="Opaque",
            immutable=True,
            data={
                "ssh_host_ed25519_key": host_private_pem.apply(_k8s_secret_data),
                "ssh_host_ed25519_key.pub": host_public_openssh.apply(
                    _k8s_secret_data
                ),
            },
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[gitea_ns],
            ),
        )

        # Single-line ``known_hosts`` entry Flux source-controller will trust.
        # Format: ``<hostname> <alg> <base64-pubkey>``.
        # ``host_public_openssh`` is already in the ``ssh-ed25519 AAAA...``
        # shape — prepending the hostname turns it into a valid
        # ``known_hosts`` line. (The optional trailing comment doesn't
        # affect matching.)
        ssh_known_hosts = host_public_openssh.apply(
            lambda pub: f"{_in_cluster_ssh_host()} {pub.strip()}\n"
        )

        # Source Secret holding the admin user's private key. Downstream
        # components copy from this Secret into their own namespaces rather
        # than accepting raw private-key material as an input.
        user_key_secret = k8s.core.v1.Secret(
            f"{name}-ssh-user-key",
            metadata={
                "name": user_key_secret_name,
                "namespace": _GITEA_NAMESPACE,
            },
            type="Opaque",
            immutable=True,
            data={
                _USER_PRIVATE_KEY_SECRET_KEY: user_private_pem.apply(
                    _k8s_secret_data
                ),
            },
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[gitea_ns],
            ),
        )

        # The Helm release itself.
        gitea = k8s.helm.v3.Release(
            f"{name}-gitea",
            chart=_GITEA_CHART_NAME,
            version=_GITEA_CHART_VERSION,
            repository_opts={"repo": _GITEA_CHART_REPO},
            namespace=_GITEA_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=600,
            values=_chart_values(
                _CREDENTIALS_SECRET, admin_email, host_key_secret_name
            ),
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[credentials_secret, host_key_secret],
            ),
        )

        # Look the chart-created Service up so we can read the LoadBalancer
        # IP that cloud-provider-kind assigned to it. ``Service.get`` is
        # the canonical "import existing k8s resource into Pulumi state"
        # call; combined with ``depends_on=[gitea]`` it correctly defers
        # the lookup until after the Helm release has finished, which in
        # turn has already awaited the LB IP assignment (pulumi-kubernetes
        # blocks on ``.status.loadBalancer.ingress`` populating for any
        # type=LoadBalancer Service the chart creates).
        gitea_http_svc = k8s.core.v1.Service.get(
            f"{name}-gitea-http-lookup",
            id=f"{_GITEA_NAMESPACE}/{_GITEA_HTTP_SERVICE}",
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[gitea],
            ),
        )

        # Resolve the LB ingress to a host-reachable ``http://IP:PORT``
        # URL. cloud-provider-kind populates ``ip`` (not ``hostname``)
        # so the conditional below is mostly defensive; the empty-string
        # fallback turns a missing LB into a deterministic error from
        # the dynamic resources downstream rather than a confusing
        # ``NoneType`` AttributeError mid-apply.
        def _build_external_base(status: Any) -> str:
            ingress = (
                getattr(getattr(status, "load_balancer", None), "ingress", None)
                if status is not None
                else None
            )
            if not ingress:
                return ""
            entry = ingress[0]
            host = getattr(entry, "ip", None) or getattr(entry, "hostname", None) or ""
            return f"http://{host}:{_GITEA_HTTP_PORT}" if host else ""

        external_base = gitea_http_svc.status.apply(_build_external_base)

        gitea_provider = gitea_sdk.Provider(
            f"{name}-provider",
            base_url=external_base,
            username=admin_username,
            password=admin_password.result,
            opts=ResourceOptions(parent=self, depends_on=[gitea]),
        )

        # Create the actual repo through Gitea's REST API, then push the
        # local working tree into it. Both are children of this component
        # so ``pulumi destroy`` cleans them up before the chart goes away.
        repo = gitea_sdk.Repository(
            f"{name}-repo",
            username=owner,
            name=repo_name,
            default_branch=default_branch,
            auto_init=False,
            private=True,
            description="Bootstrap GitOps repo managed by ca4s-infra",
            opts=ResourceOptions(
                parent=self,
                provider=gitea_provider,
                depends_on=[gitea],
                delete_before_replace=True,
            ),
        )

        # Upload the admin user's SSH public key to Gitea so PKO can
        # authenticate as the admin user over SSH (push + pull on every
        # repo). The key is attached to the admin user account, matching the
        # user the SSH URL above (``ssh://git@...``) effectively logs in as
        # via Gitea's SSH gateway.
        ssh_key = gitea_sdk.PublicKey(
            f"{name}-ssh-key",
            username=admin_username,
            title=_ADMIN_SSH_KEY_TITLE,
            key=user_public_openssh,
            read_only=False,
            opts=ResourceOptions(
                parent=self,
                provider=gitea_provider,
                depends_on=[repo],
            ),
        )

        sync = GiteaSync(
            f"{name}-sync",
            ssh_private_key=user_private_pem,
            ssh_known_hosts=ssh_known_hosts,
            owner=owner,
            repo_name=repo_name,
            default_branch=default_branch,
            source_dir=source_dir,
            kubeconfig=kubeconfig,
            service_namespace=_GITEA_NAMESPACE,
            service_name=_GITEA_SSH_SERVICE,
            service_port=_GITEA_SSH_PORT,
            triggers=sync_triggers,
            opts=ResourceOptions(
                parent=self,
                # The repo must exist and the admin SSH public key must be
                # registered before we can push over SSH.
                depends_on=[repo, ssh_key],
            ),
        )

        # Outputs. The required GitOpsRepository contract values are
        # ``url`` / ``url_external`` / ``default_branch`` /
        # ``ssh_private_key_secret`` / ``ssh_known_hosts``. The credentials
        # Secret stays an in-package detail (the Gitea chart needs it
        # for first-boot admin bootstrap) and is no longer part of the
        # contract.
        self.url = Output.from_input(_in_cluster_ssh_url(owner, repo_name))
        self.url_external = Output.concat(external_base, "/", owner, "/", repo_name, ".git")
        self.default_branch = Output.from_input(default_branch)
        self.ssh_private_key_secret = user_key_secret
        self.ssh_known_hosts = ssh_known_hosts

        flux_source = FluxGitSource(
            f"{name}-flux-source",
            provider=flux_provider,
            flux_infrastructure=flux_infrastructure,
            pko_namespace_resource=pko_namespace_resource,
            repo_url=self.url,
            repo_branch=self.default_branch,
            ssh_private_key_secret=self.ssh_private_key_secret,
            ssh_known_hosts=self.ssh_known_hosts,
            opts=ResourceOptions(parent=self, depends_on=[sync]),
        )
        self.flux_source = flux_source
        self.flux_source_name = flux_source.source_name
        self.flux_receiver_token = flux_source.receiver_token
        self.flux_receiver_url = flux_source.receiver_url

        # Built-in-provider handles used by the local stack to register the
        # Gitea -> Flux Receiver webhook. These are intentionally not part of
        # the generic GitOpsRepository contract.
        self.api_url = external_base
        self.admin_password = admin_password.result
        self.owner = Output.from_input(owner)
        self.repo_name = Output.from_input(repo_name)

        # Useful for human eyeballing of the deployed state.
        self.repo_full_name = Output.concat(owner, "/", repo_name)
        self.repo_html_url = repo.html_url
        self.sync_head_sha = sync.head_sha
        self.ssh_key_id = ssh_key.public_key_id
        self.gitea_host_key_secret_name = host_key_secret_name

        # Surface a few helpful diagnostics in addition to the contract
        # outputs — these are not part of GitOpsRepository but exist so a
        # human running ``pulumi stack output`` can see what got deployed.
        self.gitea_chart_version = Output.from_input(_GITEA_CHART_VERSION)
        self.gitea_app_version = Output.from_input(_GITEA_APP_VERSION)
        self.admin_username = Output.from_input(admin_username)

        self.webhook_args = {
            "api_url": self.api_url,
            "admin_username": self.admin_username,
            "admin_password": self.admin_password,
            "owner": self.owner,
            "repo_name": self.repo_name,
            "webhook_url": self.flux_receiver_url,
            "secret": self.flux_receiver_token,
            "events": ["push"],
        }

        # Anchor the Helm release in the component's output set so Pulumi
        # tracks readiness end-to-end and ``preview`` shows it as a child.
        self.helm_release_status = gitea.status

        self.register_outputs(
            {
                "url": self.url,
                "url_external": self.url_external,
                "default_branch": self.default_branch,
                "ssh_known_hosts": self.ssh_known_hosts,
                "flux_source_name": self.flux_source_name,
                "flux_receiver_token": self.flux_receiver_token,
                "flux_receiver_url": self.flux_receiver_url,
                "api_url": self.api_url,
                "gitea_chart_version": self.gitea_chart_version,
                "gitea_app_version": self.gitea_app_version,
                "admin_username": self.admin_username,
                "admin_password": self.admin_password,
                "owner": self.owner,
                "repo_name": self.repo_name,
                "webhook_args": self.webhook_args,
                "repo_full_name": self.repo_full_name,
                "repo_html_url": self.repo_html_url,
                "sync_head_sha": self.sync_head_sha,
                "ssh_key_id": self.ssh_key_id,
                "gitea_host_key_secret_name": self.gitea_host_key_secret_name,
            }
        )


class GiteaBuiltinWebhook(GitOpsWebhookProvider):
    """Gitea implementation of the generic GitOps webhook contract."""

    hook_id: Output[str]

    def __init__(
        self,
        name: str,
        *,
        api_url: pulumi.Input[str],
        admin_username: pulumi.Input[str],
        admin_password: pulumi.Input[str],
        owner: pulumi.Input[str],
        repo_name: pulumi.Input[str],
        webhook_url: pulumi.Input[str],
        secret: pulumi.Input[str],
        events: pulumi.Input[list[str]] | None = None,
        active: pulumi.Input[bool] = True,
        branch_filter: pulumi.Input[str] = "*",
        opts: Optional[ResourceOptions] = None,
    ) -> None:
        super().__init__(
            name,
            t="ca4s:gitrepo:GiteaBuiltinWebhook",
            opts=opts,
        )

        gitea_provider = gitea_sdk.Provider(
            f"{name}-provider",
            base_url=api_url,
            username=admin_username,
            password=admin_password,
            opts=ResourceOptions(parent=self),
        )
        webhook = gitea_sdk.RepositoryWebhook(
            f"{name}-hook",
            username=owner,
            name=repo_name,
            url=webhook_url,
            secret=secret,
            events=events if events is not None else ["push"],
            active=active,
            branch_filter=branch_filter,
            content_type="json",
            type="gitea",
            opts=ResourceOptions(parent=self, provider=gitea_provider),
        )

        self.hook_id = webhook.repository_webhook_id
        self.register_outputs({"hook_id": self.hook_id})
