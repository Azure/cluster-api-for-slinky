"""Built-in Gitea ``GitOpsRepository`` implementation.

This module stands up a self-contained, ephemeral Gitea instance inside the
management cluster and exposes it through the
:class:`~gitrepo._base.GitOpsRepository` contract so downstream Flux
``GitRepository`` resources can consume it without knowing how the git server
was created.

What we deploy
--------------
* A single namespace, ``gitea``, holding the server and the credentials
  Secret.
* A ``RandomPassword`` for the admin user. Held in Pulumi state and never
  surfaced as a stack output.
* One credentials ``Secret`` in the ``gitea`` namespace named
  ``gitea-credentials`` with two keys: ``username`` and ``password``.
  Two consumers read it:

    - The Gitea Helm chart, via ``gitea.admin.existingSecret`` — bootstraps
      the admin user on first boot. (``email`` is sourced from chart
      values directly, not from the Secret, so it isn't a Secret key.)
    - Downstream Flux ``GitRepository.spec.secretRef`` — expects exactly
      the same two keys.

  Flux only ever reads from this repo and the cluster is single-tenant
  by construction, so reusing the admin pair is fine for now.
* A ``helm.v3.Release`` of ``gitea-charts/gitea`` (pinned version below)
  configured for the minimal footprint: no postgres, no redis, no
  external cache, sqlite3 DB, memory session/cache, level queue. A
  small (2 GiB) PVC backs ``/data`` via kind's local-path provisioner
  so the seeded repo + admin DB survive pod restarts; the chart's
  ``helm.sh/resource-policy: keep`` annotation is explicitly stripped
  so ``pulumi destroy`` / ``helm uninstall`` reclaims the PVC rather
  than leaking it. The HTTP Service is exposed as ``LoadBalancer`` so
  cloud-provider-kind can publish a host-reachable address for
  ``GiteaRepo`` / ``GiteaSeed`` to use; SSH stays on ``ClusterIP``.
* A ``GiteaRepo`` (``gitrepo.gitea_repo``) that ``POST``s to
  ``/api/v1/user/repos`` and lands an empty ``<owner>/<repo_name>``
  inside Gitea. Re-adopts on 409 so partial failures are recoverable.
* A ``GiteaSeed`` (``gitrepo.gitea_seed``) that does a one-time
  ``git push --force`` of the local working tree's current ``HEAD``
  into the repo's default branch. Re-runs (replace) only when
  ``HEAD`` advances, never on URL or credential drift.

What we don't deploy (yet)
--------------------------
* No Ingress / TLS. The HTTP Service rides on cloud-provider-kind's
  per-cluster-bridge LB IP (e.g. ``172.18.0.x:3000``) — host-reachable
  but not exposed off-host. A future Flux Kustomization can drop in
  ingress-nginx + a real cert if/when needed.
* No dedicated read-only user. The seed pushes as the admin user and
  ``gitea-credentials`` reuses the admin password. If multi-tenancy
  starts to matter, introduce a ``gitea-flux`` user via the REST API
  and point the credentials Secret at that instead.

Pin policy
----------
Chart version is pinned (see ``_GITEA_CHART_VERSION``). Upgrades are an
explicit edit + ``pulumi up``, not an implicit "always-latest" drift —
matching the rest of this stack's conservative defaults.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

import pulumi
import pulumi_kubernetes as k8s
import pulumi_random as random
from pulumi import Output, ResourceOptions

from gitrepo._base import GitOpsRepository
from gitrepo.gitea_repo import GiteaRepo
from gitrepo.gitea_seed import GiteaSeed


# Pinned upstream chart. Bump this together with ``_GITEA_APP_VERSION``
# (which is informational only — surfaced for log/output use, not passed
# to the chart) when refreshing. Chart 12.6.0 ships Gitea 1.26.1.
_GITEA_CHART_REPO = "https://dl.gitea.com/charts/"
_GITEA_CHART_NAME = "gitea"
_GITEA_CHART_VERSION = "12.6.0"
_GITEA_APP_VERSION = "1.26.1"  # informational

# Namespace this component owns. We don't make it configurable — there's
# no real use case for renaming it and hard-coding keeps the Flux side
# simpler. We deliberately don't pre-create a Flux-specific namespace
# here; whoever installs Flux owns that decision.
_GITEA_NAMESPACE = "gitea"

# The single credentials Secret name. The Gitea Helm chart reads it via
# ``gitea.admin.existingSecret`` (pulling ``username`` / ``password`` keys
# out of it to bootstrap the admin user on first boot); downstream Flux
# resources read the same two keys via ``GitRepository.spec.secretRef``.
# One Secret, two consumers.
_CREDENTIALS_SECRET = "gitea-credentials"

# Gitea refuses the literal string ``admin`` as an administrator username
# (it's reserved). We pick a sane default and let the user override via
# config without ever colliding with that reservation.
_DEFAULT_ADMIN_USERNAME = "caps-admin"
_DEFAULT_ADMIN_EMAIL = "caps-admin@example.invalid"

# Repo coordinates the seed phase will use. ``_DEFAULT_REPO_OWNER`` is the
# Gitea org / user that will own the seeded repo; defaulting it to the admin
# username keeps the URL self-describing.
_DEFAULT_REPO_NAME = "cluster-api-provider-slinky"
_DEFAULT_BRANCH = "main"

# In-cluster service coordinates. The upstream Gitea chart names its HTTP
# Service ``gitea-http`` and exposes port 3000 by default; we don't override
# either, so these are the values to use in the in-cluster URL.
_GITEA_HTTP_SERVICE = "gitea-http"
_GITEA_HTTP_PORT = 3000

# Default location of the local working tree the seed pushes from.
# Computed relative to this source file: ``gitrepo/gitea_builtin.py``
# → ``gitrepo/`` → ``pulumi/`` → repo root.
# Picking the repo root by default mirrors how a developer would naturally
# invoke ``pulumi up`` from the project they're iterating on.
_DEFAULT_SOURCE_DIR = str(Path(__file__).resolve().parents[2])


def _chart_values(admin_secret_name: str, admin_email: str) -> Mapping[str, Any]:
    """Return the Helm values dict for the pinned Gitea chart.

    Kept as a module-level function (rather than inlined in ``__init__``) so
    the policy choices are easy to diff in isolation when the chart version
    moves and the values schema shifts under us. Mirrors the legacy
    ``gitea-values.yaml`` this stack replaces, minus the bits the chart
    schema has since renamed.

    ``admin_email`` is passed in (rather than read from the Secret) because
    the chart's init script sources ``email`` from
    ``.Values.gitea.admin.email`` directly — only ``username`` and
    ``password`` are wired from ``existingSecret``.
    """
    return {
        # Force the chart's ``fullname`` template to produce the static
        # string ``gitea`` instead of the default ``<release-name>-<chart-
        # name>`` combo. The chart applies this to every resource it
        # generates, so all the Service / ConfigMap / Secret names become
        # stable and predictable. Crucial because we hard-code the
        # in-cluster URL (``gitea-http.gitea.svc.cluster.local``) into the
        # GitOpsRepository ``url`` output — if the service name drifted
        # with each Pulumi release, every Flux Kustomization downstream
        # would have to re-resolve it.
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
        # bit: the chart's default value bakes in
        # ``helm.sh/resource-policy: keep``, which makes ``helm uninstall``
        # (and therefore ``pulumi destroy``) skip reclaiming the PVC.
        # That's the right call for a production stateful service \u2014 you
        # don't want a misclick to destroy years of data \u2014 but for this
        # ephemeral, re-seedable Gitea it just leaks 2 GiB per
        # destroy/up cycle. Helm v3 won't drop a default map key when
        # you pass an empty ``{}`` override (deep-merge keeps defaults);
        # the only way to actually strip the annotation is to set that
        # specific key to ``None`` (rendered as YAML ``null``), which
        # the chart template emits as an empty annotations block.
        "persistence": {
            "enabled": True,
            "size": "2Gi",
            "annotations": {
                "helm.sh/resource-policy": None,
            },
        },
        # HTTP exposed as ``LoadBalancer`` so cloud-provider-kind can
        # publish it on a host-reachable address. We need that address
        # at two points in this stack:
        #   * GiteaRepo, to call the Gitea REST admin API from the host;
        #   * GiteaSeed, to ``git push`` the local working tree.
        # SSH stays ``ClusterIP`` — we don't seed over SSH, and exposing
        # an unauthenticated SSH endpoint to the LAN would be a needless
        # foot-gun.
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
            "ssh": {"type": "ClusterIP"},
        },
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
            },
        },
    }


def _in_cluster_url(owner: str, repo: str) -> str:
    """Compute the in-cluster git HTTPS URL for ``owner/repo``.

    All three components — service name, namespace, and port — are constants
    pinned by the chart values above, so this is a pure formatting helper.
    """
    return (
        f"http://{_GITEA_HTTP_SERVICE}.{_GITEA_NAMESPACE}.svc.cluster.local"
        f":{_GITEA_HTTP_PORT}/{owner}/{repo}.git"
    )


class GiteaBuiltinRepository(GitOpsRepository):
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
    admin_username, admin_email :
        Override the defaults for the Gitea admin user. ``admin_username``
        must not be the literal ``"admin"`` (Gitea reserves it).
    repo_owner, repo_name, default_branch :
        Coordinates for the ``GiteaRepo`` / ``GiteaSeed`` children:
        which Gitea user/org owns the seeded repo, what it's called,
        and what branch the local working tree gets pushed to.
        ``repo_owner`` defaults to ``admin_username`` so the URL is
        self-describing. Surfaced verbatim into the ``url`` /
        ``url_external`` outputs.
    source_dir :
        Local git working tree the seed force-pushes from. Defaults to
        the repo root that contains this Pulumi project. ``HEAD`` of
        this directory is resolved at construction time and captured
        as a seed input — when it advances, the seed re-pushes; URL /
        credential drift alone never triggers a re-push.
    opts :
        Standard Pulumi ``ResourceOptions``.
    """

    def __init__(
        self,
        name: str,
        kubeconfig: Output[str] | str,
        *,
        admin_username: str = _DEFAULT_ADMIN_USERNAME,
        admin_email: str = _DEFAULT_ADMIN_EMAIL,
        repo_owner: Optional[str] = None,
        repo_name: str = _DEFAULT_REPO_NAME,
        default_branch: str = _DEFAULT_BRANCH,
        source_dir: str = _DEFAULT_SOURCE_DIR,
        opts: Optional[ResourceOptions] = None,
    ) -> None:
        super().__init__(
            name,
            t="ca4s:gitrepo:GiteaBuiltinRepository",
            opts=opts,
        )

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

        # The single credentials Secret. Two consumers:
        #
        #   * The Gitea Helm chart, via ``gitea.admin.existingSecret`` —
        #     reads ``username``/``password`` on first boot and runs
        #     ``gitea admin user create``. Order matters: must exist
        #     before the Helm release reaches its admin-init job.
        #   * Downstream Flux ``GitRepository.spec.secretRef`` — reads
        #     the same two keys. Lives in the ``gitea`` namespace
        #     alongside the server, *not* in a separate ``flux-system``
        #     namespace. Rationale: ``secretRef`` is always namespace-
        #     local, so whoever consumes these credentials (Flux, AWX,
        #     anything else) will need a copy in *its* namespace anyway.
        #     Putting it next to the server it authenticates against is
        #     the honest place to expose it; consumers handle propagation.
        #
        # Type ``Opaque`` (not ``kubernetes.io/basic-auth``) because Flux
        # documents ``Opaque`` with ``username``/``password`` keys as the
        # canonical shape; using the typed flavor works too but is less
        # explicit when grepping by key.
        credentials_secret = k8s.core.v1.Secret(
            f"{name}-credentials",
            metadata={
                "name": _CREDENTIALS_SECRET,
                "namespace": _GITEA_NAMESPACE,
            },
            type="Opaque",
            string_data={
                "username": admin_username,
                "password": admin_password.result,
            },
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[gitea_ns],
                # Preserve Pulumi state across the rename from the
                # earlier two-Secret layout so this is an in-place
                # update rather than a destroy+recreate.
                aliases=[pulumi.Alias(name=f"{name}-flux-credentials")],
            ),
        )

        # The Helm release itself. ``skip_await=False`` (default) makes
        # Pulumi block on chart readiness; ``cleanup_on_fail=True`` keeps
        # failed installs from leaving dangling resources that confuse the
        # next ``pulumi up``.
        gitea = k8s.helm.v3.Release(
            f"{name}-gitea",
            chart=_GITEA_CHART_NAME,
            version=_GITEA_CHART_VERSION,
            repository_opts={"repo": _GITEA_CHART_REPO},
            namespace=_GITEA_NAMESPACE,
            cleanup_on_fail=True,
            values=_chart_values(_CREDENTIALS_SECRET, admin_email),
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[credentials_secret],
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

        # Create the actual repo through Gitea's REST API, then push the
        # local working tree into it. Both are children of this component
        # so ``pulumi destroy`` cleans them up before the chart goes
        # away (otherwise the API endpoint would be unreachable when
        # GiteaRepo.delete tries to call it).
        repo = GiteaRepo(
            f"{name}-repo",
            api_url=external_base,
            admin_username=admin_username,
            admin_password=admin_password.result,
            owner=owner,
            repo_name=repo_name,
            default_branch=default_branch,
            opts=ResourceOptions(
                parent=self,
                depends_on=[gitea],
                # Server-side ID is ``<owner>/<repo_name>``, invariant
                # across replacements. Pulumi's default create-before-
                # delete would have the delete of the "old" resource
                # ``DELETE`` the new one we just ``POST``'d (same path).
                # Force delete-before-create so replacement is safe.
                delete_before_replace=True,
            ),
        )

        seed = GiteaSeed(
            f"{name}-seed",
            api_url=external_base,
            admin_username=admin_username,
            admin_password=admin_password.result,
            owner=owner,
            repo_name=repo_name,
            default_branch=default_branch,
            source_dir=source_dir,
            opts=ResourceOptions(
                parent=self,
                # The repo must exist before we push into it. ``git push``
                # to a non-existent Gitea repo returns 404.
                depends_on=[repo],
            ),
        )

        # Outputs. All five are required by the GitOpsRepository contract.
        # ``url`` is the in-cluster URL Flux will pull from; ``url_external``
        # is the host-reachable URL the seed uses (and a developer can
        # also browse to in a web browser).
        in_cluster = _in_cluster_url(owner, repo_name)
        self.url = Output.from_input(in_cluster)
        self.url_external = Output.concat(external_base, "/", owner, "/", repo_name, ".git")
        self.default_branch = Output.from_input(default_branch)
        self.credentials_secret_name = Output.from_input(_CREDENTIALS_SECRET)
        self.credentials_secret_namespace = Output.from_input(_GITEA_NAMESPACE)

        # Useful for human eyeballing of the deployed state.
        self.repo_full_name = repo.full_name
        self.repo_html_url = repo.html_url
        self.seed_head_sha = seed.head_sha

        # Surface a few helpful diagnostics in addition to the contract
        # outputs — these are not part of GitOpsRepository but exist so a
        # human running ``pulumi stack output`` can see what got deployed.
        self.gitea_chart_version = Output.from_input(_GITEA_CHART_VERSION)
        self.gitea_app_version = Output.from_input(_GITEA_APP_VERSION)
        self.admin_username = Output.from_input(admin_username)

        # Anchor the Helm release in the component's output set so Pulumi
        # tracks readiness end-to-end and ``preview`` shows it as a child.
        self.helm_release_status = gitea.status

        self.register_outputs(
            {
                "url": self.url,
                "url_external": self.url_external,
                "default_branch": self.default_branch,
                "credentials_secret_name": self.credentials_secret_name,
                "credentials_secret_namespace": self.credentials_secret_namespace,
                "gitea_chart_version": self.gitea_chart_version,
                "gitea_app_version": self.gitea_app_version,
                "admin_username": self.admin_username,
                "repo_full_name": self.repo_full_name,
                "repo_html_url": self.repo_html_url,
                "seed_head_sha": self.seed_head_sha,
            }
        )
