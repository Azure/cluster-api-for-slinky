"""Pulumi dynamic resource: a local Kubernetes cluster managed by `ctlptl`.

Design notes
------------
* The resource is identified by its ctlptl ``Cluster.name`` (e.g.
  ``kind-mgmt-a1b2c3d4``). That name becomes the Pulumi resource ID and the
  kubeconfig context name (kind always prefixes its contexts with ``kind-``,
  which is why ctlptl's ``Cluster.name`` for product=kind must start with
  ``kind-`` too).
* The cluster is wired to separately-managed ``CtlptlRegistry`` resources by
    mounting generated containerd ``hosts.toml`` files into the kind nodes and
    attaching the registry containers to Docker's ``kind`` network after cluster
    creation. Passing registry ``registry_name`` outputs into this resource gives
    Pulumi the dependency edges, so registries are created first and deleted last.

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
— callers no longer pass it in. Customizability is explicitly deferred; if
you need to tweak the kind config, edit the template constant below.

The template contains two shell-style placeholders that the provider
substitutes when rendering the manifest:

* ``${CLUSTER_NAME}`` → the autonamed or pinned ``cluster_name``. Must be
  expanded inside the provider because the autonamed value only exists
  inside ``create()``.
* ``${DOCKER_IO_HOSTS_TOML}`` → generated ``hosts.toml`` file mounted into
    kind nodes for containerd's Docker Hub pull-through registry cache config.
* ``${CUSTOM_REGISTRY_MOUNTS}`` → generated extraMount entries for direct
    HTTP custom registry endpoints such as ``custom-registry:5000``.

The ``registry_name`` input is rendered into that generated Docker Hub
``hosts.toml`` file. The ctlptl ``Cluster`` manifest does not set its own
``registry`` field because that path lets ctlptl recreate the sibling registry
without the proxy env, destroying the retained cache.

* Inputs:
    - ``cluster_name``  : optional explicit ctlptl ``Cluster.name`` value.
                          When omitted, autoname kicks in.
    - ``registry_name`` : optional sibling registry name rendered into the
                          generated Docker Hub ``hosts.toml``. Pass
                          ``CtlptlRegistry().registry_name`` to wire the
                          dependency implicitly. Required because Docker Hub
                          pulls are routed through the sibling registry cache.
    - ``custom_registry_names``: optional sibling custom registry names exposed
                          to containerd as direct HTTP registries and connected
                          to the kind Docker network.
* Outputs:
    - ``cluster_name`` : the ctlptl ``Cluster.name`` ultimately used
                         (autonamed or explicit).
    - ``kubeconfig``   : standalone kubeconfig YAML for the new cluster.
    - ``context``      : kube context name (== ``cluster_name``).
* Lifecycle:
    - ``check``  : validates explicit ``cluster_name`` values. Autonaming still
                   happens in ``create()`` so previews stay deterministic.
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
path (``ctlptl.ctlptl_cluster``) keeps that round-trip robust.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from pulumi import Input, Output, ResourceOptions
from pulumi.dynamic import (
    CheckFailure,
    CheckResult,
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
)

_RESOURCE_TYPE = "ca4s:local:CtlptlCluster"
_GENERATED_CONFIG_DIR = Path(__file__).resolve().parents[1] / ".state" / "ctlptl"

# ---------------------------------------------------------------------------
# Vendored ctlptl manifest.
#
# Kept in source so the Pulumi stack does not need a separate manifest file.
# Customizability is deferred; edit this constant directly to change the kind
# topology. Placeholders are substituted by ``_render()``.
# ---------------------------------------------------------------------------

_MANIFEST_TEMPLATE = """\
---
apiVersion: ctlptl.dev/v1alpha1
kind: Cluster
product: kind
name: ${CLUSTER_NAME}
kindV1Alpha4Cluster:
  apiVersion: kind.x-k8s.io/v1alpha4
  kind: Cluster
  networking:
    ipFamily: dual
  nodes:
  - role: control-plane
    image: kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5
    extraMounts:
    # Docker Hub mirror for in-cluster pod pulls. Host Docker still handles the
    # node image pull before these containers exist.
    - hostPath: ${DOCKER_IO_HOSTS_TOML}
      containerPath: /etc/containerd/certs.d/docker.io/hosts.toml
      readOnly: true
${CUSTOM_REGISTRY_MOUNTS}
    # required by CAPD
    - hostPath: /var/run/docker.sock
      containerPath: /var/run/docker.sock
  - role: worker
    image: kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5
    extraMounts:
    # Docker Hub mirror for in-cluster pod pulls. Host Docker still handles the
    # node image pull before these containers exist.
    - hostPath: ${DOCKER_IO_HOSTS_TOML}
      containerPath: /etc/containerd/certs.d/docker.io/hosts.toml
      readOnly: true
${CUSTOM_REGISTRY_MOUNTS}
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


def _docker_io_hosts_toml(cluster_name: str, registry_name: str) -> Path:
    """Write the containerd Docker Hub hosts config for this kind cluster."""
    path = _GENERATED_CONFIG_DIR / cluster_name / "docker.io" / "hosts.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "server = \"https://registry-1.docker.io\"\n\n"
        f"[host.\"http://{registry_name}:5000\"]\n"
        "  capabilities = [\"pull\", \"resolve\"]\n",
        encoding="utf-8",
    )
    return path


def _registry_hosts_toml(cluster_name: str, registry_name: str) -> Path:
    """Write the containerd hosts config for an in-network HTTP registry."""
    endpoint = f"{registry_name}:5000"
    path = _GENERATED_CONFIG_DIR / cluster_name / endpoint / "hosts.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"server = \"http://{endpoint}\"\n\n"
        f"[host.\"http://{endpoint}\"]\n"
        "  capabilities = [\"pull\", \"resolve\"]\n",
        encoding="utf-8",
    )
    return path


def _custom_registry_mounts(
    cluster_name: str,
    registry_names: list[str],
) -> str:
    mounts: list[str] = []
    for registry_name in registry_names:
        hosts_path = _registry_hosts_toml(cluster_name, registry_name)
        mounts.extend(
            [
                f"    - hostPath: {hosts_path}",
                f"      containerPath: /etc/containerd/certs.d/{registry_name}:5000/hosts.toml",
                "      readOnly: true",
            ]
        )
    if not mounts:
        return ""
    return "\n".join(mounts) + "\n"


def _registry_names(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    names: list[str] = []
    for item in value:
        name = str(item)
        if name and name not in names:
            names.append(name)
    return names


def _connect_registry_to_kind_network(registry_name: str) -> None:
    """Attach the managed registry container to Docker's kind network."""
    _require_binary("docker")
    result = _run(
        ["docker", "network", "connect", "kind", registry_name],
        check=False,
    )
    if result.returncode == 0:
        return
    stderr = result.stderr or ""
    if "already exists" in stderr or "is already attached" in stderr:
        return
    raise RuntimeError(
        "failed to attach registry container "
        f"{registry_name!r} to Docker network 'kind': {stderr.strip()}"
    )


# ---------------------------------------------------------------------------
# Provider.
# ---------------------------------------------------------------------------


def _render(
    cluster_name: str,
    registry_name: Optional[str] = None,
    custom_registry_names: Optional[list[str]] = None,
) -> str:
    """Render the vendored manifest with all placeholders substituted.

    Substitution policy:
    * ``${CLUSTER_NAME}`` → ``cluster_name``. Done here because the
      autonamed value only exists inside ``create()``.
        * ``registry_name`` → rendered into the generated Docker Hub hosts file,
            not the ctlptl ``Cluster`` spec. Accepting it here lets the caller pass
            ``CtlptlRegistry().registry_name`` directly as an ``Input[str]``, giving
            Pulumi the cross-resource dependency edge for free (no ``depends_on``
            needed).
        * ``${DOCKER_IO_HOSTS_TOML}`` → generated hostPath file pointing Docker Hub
            pulls at the sibling ctlptl registry's in-cluster address.

        A missing ``registry_name`` is rejected here because Docker Hub pull-through
        cache config depends on the sibling registry's in-cluster name.
    """
    if not registry_name:
        raise RuntimeError(
            "registry_name is required to render kind registry cache config"
        )
    docker_io_hosts_toml = _docker_io_hosts_toml(cluster_name, registry_name)
    custom_registry_mounts = _custom_registry_mounts(
        cluster_name,
        custom_registry_names or [],
    )
    rendered = _MANIFEST_TEMPLATE
    rendered = rendered.replace("${CLUSTER_NAME}", cluster_name)
    rendered = rendered.replace(
        "${DOCKER_IO_HOSTS_TOML}", str(docker_io_hosts_toml)
    )
    rendered = rendered.replace(
        "${CUSTOM_REGISTRY_MOUNTS}",
        custom_registry_mounts,
    )
    return rendered


class _CtlptlClusterProvider(ResourceProvider):
    """Lifecycle hooks for the CtlptlCluster dynamic resource."""

    def check(self, olds: dict, news: dict) -> CheckResult:
        failures: list[CheckFailure] = []
        cluster_name = news.get("cluster_name")
        if cluster_name and not cluster_name.startswith("kind-"):
            failures.append(
                CheckFailure(
                    "cluster_name",
                    "ctlptl kind cluster names must start with 'kind-'",
                )
            )
        return CheckResult(inputs=news, failures=failures)

    def create(self, props: dict) -> CreateResult:
        _require_binary("ctlptl")
        # Resolve the cluster name: caller's pin, or autoname. The
        # ``kind-`` prefix is required by ctlptl for product=kind so the
        # resulting kubectl context name matches what kind itself would mint.
        cluster_name: str = props.get("cluster_name") or (
            f"kind-{props.get('_autoname_seed') or 'mgmt'}-{secrets.token_hex(4)}"
        )
        registry_name: Optional[str] = props.get("registry_name")
        custom_registry_names = _registry_names(props.get("custom_registry_names"))
        rendered = _render(cluster_name, registry_name, custom_registry_names)

        _run(["ctlptl", "apply", "-f", "-"], stdin=rendered)
        for connected_registry_name in _registry_names(
            [registry_name, *custom_registry_names]
        ):
            _connect_registry_to_kind_network(connected_registry_name)
        kubeconfig = _fetch_kubeconfig(cluster_name)

        return CreateResult(
            id_=cluster_name,
            outs={
                "cluster_name": cluster_name,
                "registry_name": registry_name,
                "custom_registry_names": custom_registry_names,
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
        if _registry_names(news.get("custom_registry_names")) != _registry_names(
            olds.get("custom_registry_names")
        ):
            replaces.append("custom_registry_names")

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
    sibling ``CtlptlRegistry``. The provider renders the value into the
    generated Docker Hub hosts file, and Pulumi's Output→Input machinery
    tracks the dependency — no explicit ``depends_on`` needed.

    Pass ``custom_registry_names=[registry.registry_name]`` for direct HTTP
    custom registries that should be reachable as ``<registry-name>:5000`` from pods.
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
        custom_registry_names: Optional[Input[List[Input[str]]]] = None,
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
                # renders it into the generated Docker Hub hosts file.
                # Passing the Output here also wires the cross-resource
                # dependency without an explicit ``depends_on``.
                "registry_name": registry_name,
                # Optional direct HTTP custom registries exposed to containerd as
                # <registry-name>:5000 and connected to the kind Docker network.
                "custom_registry_names": custom_registry_names,
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
