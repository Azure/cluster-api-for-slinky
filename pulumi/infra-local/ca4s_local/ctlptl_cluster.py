"""Pulumi dynamic resource: a local Kubernetes cluster managed by `ctlptl`.

Design notes
------------
* The resource is identified by its ctlptl ``Cluster.name`` (e.g.
  ``kind-mgmt-a1b2c3d4``). That name becomes the Pulumi resource ID and the
  kubeconfig context name (kind always prefixes its contexts with ``kind-``,
  which is why ctlptl's ``Cluster.name`` for product=kind must start with
  ``kind-`` too).
* The cluster is wired to a separately-managed ``CtlptlRegistry`` via the
  ``registry: <name>`` field in the Cluster spec. The caller is responsible
  for declaring a ``depends_on`` edge so the registry exists before the
  cluster is applied (and survives until after the cluster is deleted).

Auto-naming
-----------
Following the Pulumi convention, ``cluster_name`` is auto-generated unless
the caller pins it explicitly:

* If ``cluster_name`` is omitted, ``create()`` derives a name of the form
  ``kind-<pulumi-logical-name>-<8-char-hex>`` on first apply and stores it
  in the ``cluster_name`` output. Pulumi persists that output in state so
  subsequent ``pulumi up`` runs reuse the same name (no churn). The
  ``kind-`` prefix is mandatory for ctlptl's kind product.
* If ``cluster_name`` is supplied, that value is used verbatim and the
  random suffix is skipped.

Randomness lives in ``create()`` (not ``check()``) because ``check()`` runs
on every preview and every up; generating randomness there would yield
non-deterministic preview output and is non-idiomatic. ``create()`` runs
once per resource lifetime and Pulumi persists its outputs, so randomness
there is naturally one-shot. This mirrors the canonical "random number
generator" example in the Pulumi dynamic-provider docs.

The autonamed value is surfaced via the ``cluster_name`` / ``context``
outputs (they're the same string). ctlptl's ``apply`` switches the current
kubeconfig context to the new cluster, so ``kubectl`` users don't need to
remember the suffix; they can also read it from the ``context`` stack output.

Diff semantics for ``cluster_name``
  Because the autonamed value lives in ``olds`` after first apply but
  ``news.cluster_name`` is ``None`` whenever the caller hasn't pinned one,
  ``diff()`` treats an unpinned ``news`` as "don't care, keep what's there"
  and only forces a replacement when the caller explicitly pins a *new*
  value that differs from what state currently holds.

Manifest
--------
The ctlptl manifest is vendored into this module as ``_MANIFEST_TEMPLATE``
— callers no longer pass it in. This keeps the Pulumi program self-contained
(no repo-relative file reads from inside Pulumi). Customizability is
explicitly deferred; if you need to tweak the kind config, edit the
template constant below.

The template contains three shell-style placeholders that the provider
substitutes at apply/diff time:

* ``${CLUSTER_NAME}`` → the autonamed or pinned ``cluster_name``. Must be
  expanded inside the provider because the autonamed value only exists
  inside ``create()``.
* ``${REGISTRY_NAME}`` → the value of the ``registry_name`` input
  (typically ``CtlptlRegistry().registry_name``). Substituting inside the
  provider lets the caller pass an ``Output[str]`` directly, giving us the
  canonical Pulumi Output→Input dependency edge without an explicit
  ``depends_on``.
* ``${HOME}`` → read from the program's environment at apply time. It
  expands to a host-local filesystem path (used for ``hostPath`` mounts);
  the provider runs in the user's environment, so reading ``$HOME`` there
  is correct.

* Inputs:
    - ``cluster_name``  : optional explicit ctlptl ``Cluster.name`` value.
                          When omitted, autoname kicks in.
    - ``registry_name`` : optional sibling registry name to substitute for
                          ``${REGISTRY_NAME}``. Pass
                          ``CtlptlRegistry().registry_name`` to wire the
                          dependency implicitly. When omitted, the
                          placeholder flows through verbatim and ctlptl
                          will reject the manifest.
* Outputs:
    - ``cluster_name`` : the ctlptl ``Cluster.name`` ultimately used
                         (autonamed or explicit).
    - ``kubeconfig``   : standalone kubeconfig YAML for the new cluster.
    - ``context``      : kube context name (== ``cluster_name``).
* Lifecycle:
    - ``check``  : pure pass-through (the SDK default).
    - ``create`` : resolves the cluster name (pinned value or freshly
                   minted ``kind-<seed>-<hex>``), renders the vendored
                   manifest, runs ``ctlptl apply -f -`` on stdin, then
                   harvests the kubeconfig for that name via kubectl.
    - ``delete`` : ``ctlptl delete cluster <id>`` — the registry is owned by
                   a separate Pulumi resource and is torn down by its own
                   provider's ``delete``.
    - ``diff``   : only checks whether the caller has pinned a *new*
                   ``cluster_name`` (forces replace if so). Vendored
                   template edits are NOT auto-detected on a local stack;
                   the developer editing the template is expected to
                   ``pulumi destroy && pulumi up`` (or
                   ``pulumi up --target-replace urn:...``). On a
                   single-host dev stack the cost-benefit of manifest
                   hashing doesn't pencil out: edits are rare and the
                   developer already knows they need a fresh cluster.
                   ``delete_before_replace`` stays true because the
                   underlying kind container can't coexist with itself
                   on the same name.
    - ``read``   : verify the cluster still exists by querying
                   ``ctlptl get cluster <id>``; refresh the kubeconfig.

Why a dynamic provider instead of ``command.local.Command``?
Real lifecycle semantics: a pinned ``cluster_name`` change should surface as
a replacement plan in ``pulumi preview`` (with proper ``delete_before_replace``
ordering, since the underlying kind container can't coexist with itself on
the same name), and ``read`` lets ``pulumi refresh`` detect out-of-band
deletion.

Pickling note
-------------
Pulumi serializes the provider class into stack state via cloudpickle. Keeping
this module dependency-light (stdlib + ``pulumi`` only) and stable in module
path (``ca4s_local.ctlptl_cluster``) keeps that round-trip robust.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from typing import List, Optional

from pulumi import Input, Output, ResourceOptions
from pulumi.dynamic import (
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
)

_RESOURCE_TYPE = "ca4s:local:CtlptlCluster"

# ---------------------------------------------------------------------------
# Vendored ctlptl manifest.
#
# Kept in source so the Pulumi stack has no repo-relative file dependencies.
# Customizability is deferred; edit this constant directly to change the kind
# topology. Placeholders ``${HOME}`` / ``${CLUSTER_NAME}`` / ``${REGISTRY_NAME}``
# are substituted by ``_render()``.
# ---------------------------------------------------------------------------

_MANIFEST_TEMPLATE = """\
---
apiVersion: ctlptl.dev/v1alpha1
kind: Cluster
product: kind
name: ${CLUSTER_NAME}
registry: ${REGISTRY_NAME}
kindV1Alpha4Cluster:
  apiVersion: kind.x-k8s.io/v1alpha4
  kind: Cluster
  networking:
    ipFamily: dual
  nodes:
  - role: control-plane
    image: kindest/node:v1.34.0@sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a
    extraMounts:
    # required by CAPD
    - hostPath: /var/run/docker.sock
      containerPath: /var/run/docker.sock
  - role: worker
    image: kindest/node:v1.34.0@sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a
    extraMounts:
    # Exposes the host kubeconfig to in-cluster consumers via hostPath /root/.kube:
    #   - AWX / ansible-runner (mounted at /runner/.kube, see awx.yaml, pod-spec-override.yaml)
    #   - cluster-autoscaler   (mounted at /mnt/kubeconfig, see cluster-autoscaler.yaml)
    - hostPath: ${HOME}/.kube
      containerPath: /root/.kube
    # required by CAPD (capd-controller-manager may be scheduled on either kind node)
    - hostPath: /var/run/docker.sock
      containerPath: /var/run/docker.sock
"""


# ---------------------------------------------------------------------------
# Helpers (module-level so the unpickled provider can resolve them).
# ---------------------------------------------------------------------------


def _require_binary(name: str) -> str:
    """Return the absolute path of *name* or raise with a clear message."""
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(
            f"required binary '{name}' not found in PATH; install it before running pulumi"
        )
    return path


def _run(
    cmd: List[str],
    *,
    stdin: Optional[str] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run that captures + decodes output."""
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        check=check,
    )


def _fetch_kubeconfig(context: str) -> str:
    """Extract a self-contained kubeconfig for *context* as a YAML string."""
    _require_binary("kubectl")
    result = _run(
        [
            "kubectl",
            "config",
            "view",
            "--raw",
            "--minify",
            "--context",
            context,
            "-o",
            "yaml",
        ]
    )
    return result.stdout


# ---------------------------------------------------------------------------
# Provider.
# ---------------------------------------------------------------------------


def _render(cluster_name: str, registry_name: Optional[str] = None) -> str:
    """Render the vendored manifest with all placeholders substituted.

    Substitution policy:
    * ``${CLUSTER_NAME}`` → ``cluster_name``. Done here because the
      autonamed value only exists inside ``create()``.
    * ``${REGISTRY_NAME}`` → ``registry_name`` when truthy. Doing it here
      lets the caller pass ``CtlptlRegistry().registry_name`` directly as
      an ``Input[str]``, giving Pulumi the cross-resource dependency edge
      for free (no ``depends_on`` needed).
    * ``${HOME}`` → the program's ``$HOME`` env var, raised if missing.

    A missing ``registry_name`` leaves the placeholder verbatim; ctlptl
    will then reject the manifest. That's intentional — it surfaces the
    missing wiring loudly rather than silently substituting an empty string.
    """
    home = os.environ.get("HOME")
    if not home:
        raise RuntimeError(
            "HOME environment variable is not set; required for ctlptl manifest "
            "${HOME} substitution (hostPath mount of ~/.kube into the worker)"
        )
    rendered = _MANIFEST_TEMPLATE
    rendered = rendered.replace("${CLUSTER_NAME}", cluster_name)
    if registry_name:
        rendered = rendered.replace("${REGISTRY_NAME}", registry_name)
    rendered = rendered.replace("${HOME}", home)
    return rendered


class _CtlptlClusterProvider(ResourceProvider):
    """Lifecycle hooks for the CtlptlCluster dynamic resource."""

    # ``check`` is intentionally the SDK default (pass-through). Random
    # ``cluster_name`` generation lives in ``create()`` so it runs exactly
    # once per resource lifetime; see the module docstring for why.

    def create(self, props: dict) -> CreateResult:
        _require_binary("ctlptl")
        # Resolve the cluster name: caller's pin, or autoname. The
        # ``kind-`` prefix is required by ctlptl for product=kind so the
        # resulting kubectl context name matches what kind itself would mint.
        cluster_name: str = props.get("cluster_name") or (
            f"kind-{props.get('_autoname_seed') or 'mgmt'}-{secrets.token_hex(4)}"
        )
        registry_name: Optional[str] = props.get("registry_name")
        rendered = _render(cluster_name, registry_name)

        _run(["ctlptl", "apply", "-f", "-"], stdin=rendered)
        kubeconfig = _fetch_kubeconfig(cluster_name)

        return CreateResult(
            id_=cluster_name,
            outs={
                "cluster_name": cluster_name,
                "registry_name": registry_name,
                "kubeconfig": kubeconfig,
                "context": cluster_name,
            },
        )

    def delete(self, id_: str, props: dict) -> None:
        _require_binary("ctlptl")
        # The cluster's registry sibling (CtlptlRegistry) owns the registry
        # container and is deleted by its own provider in reverse-topo order.
        # We therefore scope this call to the cluster only.
        _run(["ctlptl", "delete", "cluster", id_], check=False)

    def diff(self, id_: str, olds: dict, news: dict) -> DiffResult:
        replaces: List[str] = []
        # ``cluster_name``: only compare when the caller actually pinned
        # one. An unpinned ``news`` (``None``/empty) means "don't care,
        # keep what was minted by ``create()`` last time."
        pinned_name = news.get("cluster_name")
        if pinned_name and pinned_name != olds.get("cluster_name"):
            replaces.append("cluster_name")
        # ``registry_name`` changes force replacement because kind bakes
        # the registry endpoint into the node's containerd config at
        # create time — there's no in-place rewire.
        if news.get("registry_name") != olds.get("registry_name"):
            replaces.append("registry_name")

        return DiffResult(
            changes=bool(replaces),
            replaces=replaces,
            # kind itself has no in-place reconfiguration verb: extraMounts,
            # extraPortMappings, node image, and networking are all baked into
            # the kind-node Docker container at create time, and Docker does
            # not allow mutating HostConfig.Binds / PortBindings after
            # creation. ctlptl therefore implements "apply with changes" as
            # delete-then-recreate, and we mirror that here.
            delete_before_replace=True,
        )

    def read(self, id_: str, props: dict) -> ReadResult:
        _require_binary("ctlptl")
        result = _run(
            [
                "ctlptl",
                "get",
                "cluster",
                id_,
                "-o",
                "template",
                # ctlptl marshals through JSON before templating, so the
                # template field names are lowercase JSON keys (NOT the
                # PascalCase Go struct field names). ``{{.Name}}`` silently
                # renders as ``<no value>`` and would falsely flag the
                # cluster as deleted on every ``pulumi refresh``.
                "--template",
                "{{.name}}",
            ],
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != id_:
            # Cluster was deleted out-of-band; signal "no longer exists".
            return ReadResult(id_=None, outs={})

        outs = dict(props)
        try:
            outs["kubeconfig"] = _fetch_kubeconfig(id_)
        except Exception:
            # Refresh is best-effort; keep the previously-known value.
            outs.setdefault("kubeconfig", "")
        return ReadResult(id_=id_, outs=outs)


# ---------------------------------------------------------------------------
# Resource class consumed by user code.
# ---------------------------------------------------------------------------


class CtlptlCluster(Resource):
    """A local kind cluster managed via ctlptl.

    The manifest is vendored into the provider module (no caller-supplied
    template). The ctlptl ``Cluster.name`` is autonamed by default (Pulumi
    convention); pass ``cluster_name="..."`` to pin it (must begin with
    ``kind-`` for product=kind).

    Pass ``registry_name=registry.registry_name`` to wire this cluster to a
    sibling ``CtlptlRegistry``. The provider substitutes the value into
    ``${REGISTRY_NAME}`` inside the manifest, and Pulumi's Output→Input
    machinery tracks the dependency — no explicit ``depends_on`` needed.
    """

    cluster_name: Output[str]
    registry_name: Output[str]
    kubeconfig: Output[str]
    context: Output[str]

    def __init__(
        self,
        name: str,
        cluster_name: Optional[Input[str]] = None,
        registry_name: Optional[Input[str]] = None,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            _CtlptlClusterProvider(),
            name,
            {
                # Inputs:
                # ``cluster_name`` may be None — ``create()`` autonames in
                # that case by combining ``_autoname_seed`` with a random
                # suffix. The value is then stored in state.
                "cluster_name": cluster_name,
                # ``registry_name`` may be None — when supplied (typically
                # ``CtlptlRegistry().registry_name``), the provider
                # substitutes it into the manifest's ``${REGISTRY_NAME}``
                # placeholder. Passing the Output here also wires the
                # cross-resource dependency without an explicit
                # ``depends_on``.
                "registry_name": registry_name,
                # Hidden seed used by ``create()`` for autoname derivation.
                # Sourced from the Pulumi logical name so the autonamed
                # value is greppable (e.g. "kind-mgmt-a1b2c3d4").
                "_autoname_seed": name,
                # Output placeholders. Declaring them as None tells Pulumi
                # these keys are outputs of this resource so downstream
                # `depends_on` / Output access works correctly.
                "kubeconfig": None,
                "context": None,
            },
            opts,
        )
