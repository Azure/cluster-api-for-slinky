# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Pulumi dynamic resource: a local image registry managed by `ctlptl`.

Design notes
------------
Models ctlptl's ``Registry`` kind as a first-class Pulumi resource. By
default ctlptl picks the host port itself when the manifest omits ``port``
(or sets it to ``0``).

Auto-naming
-----------
Following the Pulumi convention used by ``azure-native`` resources, etc.,
the underlying ``Registry.name`` is auto-generated unless
the caller pins it explicitly:

* If ``registry_name`` is omitted, ``create()`` derives a name of the form
  ``<pulumi-logical-name>-<8-char-hex>`` on first apply and stores it in
  the ``registry_name`` output. Pulumi persists that output in state, so
  subsequent ``pulumi up`` runs reuse the same name (no churn).
* If ``registry_name`` is supplied, that value is used verbatim and the
  random suffix is skipped.

Why randomness lives in ``create()`` (not ``check()``)
  ``check()`` runs on every preview and every up; making it generate a new
  random value each time would produce non-deterministic preview output and
  is non-idiomatic. ``create()`` runs exactly once per resource lifetime
  and Pulumi stores its outputs in state, so randomness there is naturally
  one-shot. This mirrors the canonical "random number generator" example
  in the Pulumi docs, where ``crypto.randomBytes(...)`` is called inside
  ``create()`` and surfaced as the resource ID. The trade-off is that
  ``pulumi preview`` shows ``registry_name: [unknown]`` before the first
  ``up`` — that's accurate, because the value genuinely isn't determined
  until ``create()`` runs.

Diff semantics for ``registry_name``
  Because the autonamed value lives in ``olds`` after first apply but
  ``news.registry_name`` is ``None`` whenever the caller hasn't pinned one,
  ``diff()`` treats an unpinned ``news`` as "don't care, keep what's there"
  and only forces a replacement when the caller explicitly pins a *new*
  value that differs from what state currently holds.

The autonamed value is surfaced as the ``registry_name`` output so the
management cluster can route containerd's Docker Hub pulls through the registry
by its actual Docker container name.

Port semantics
--------------
``port`` is both an input and an output:

* Pass ``port=N`` (with N > 0) to pin the registry to host port ``N``.
  Changing the pinned value forces replacement (kind container port
  bindings are immutable; ctlptl itself recreates the container on port
  change).
* Omit ``port`` or pass ``port=0`` (both mean "unpinned") to let ctlptl
  pick a free one via ``freeport``. This matches ctlptl's own manifest
  convention where ``port: 0`` and an omitted ``port:`` field are
  equivalent. To keep ``pulumi up`` stable, ``check()`` carries the
  previously-bound port forward into the inputs of subsequent runs when
  the caller has unpinned, so a transition from ``port=N`` to ``port=0``
  (or to ``None``) doesn't trigger a spurious replace — this mirrors
  ctlptl's own ``Apply`` behavior where ``desired.Port == 0`` preserves
  the existing port.
* The host-published registry port is always bound to ``0.0.0.0`` instead
    of ctlptl's default ``127.0.0.1`` so containers on other Docker bridge
    networks can reach it through a host-routable address.

Registry environment semantics
------------------------------
Set ``env`` to configure environment variables on the registry container via
ctlptl's ``Registry.env`` field. By default, the registry runs as a
pull-through cache backed by Google's Docker Hub mirror using
``REGISTRY_PROXY_REMOTEURL=https://mirror.gcr.io``. Pass an explicit ``env``
list to override the registry container environment.

Adoption and destroy semantics
------------------------------
Two lifecycle knobs model the cases where a registry is shared across local
stack lifetimes:

* ``adopt_existing`` defaults to true and asks ``create()`` to scan existing
    ctlptl registries and
    adopt the first whose name starts with ``registry_name``. If
    ``registry_name`` is empty or omitted, the first existing registry is
    adopted and the ``adopted`` output is true. If nothing matches, creation
    falls back to the normal apply path and ``adopted`` is false.
* ``delete_on_destroy`` defaults to false and controls whether ``delete()``
    tears down the underlying registry. If ``adopted`` is true, deletion is
    always skipped for robustness.

* The resource is identified by the autonamed, pinned, or adopted
    ``registry_name``.
* Inputs:
    - ``registry_name`` : optional explicit ctlptl ``Registry.name`` value.
                          When omitted, autoname kicks in.
    - ``port``          : optional host port to bind. ``None`` and ``0``
                          both mean "unpinned" — ctlptl picks a free port
                          and ``check()`` carries the observed value
                          forward on subsequent runs.
    - ``env``           : optional list of environment variables to pass to
                          the registry container. Defaults to pull-through
                          cache mode backed by ``mirror.gcr.io``.
    - ``adopt_existing``: optional bool, default ``True``. When true, adopt
                          the first existing registry whose name starts with
                          ``registry_name``; an empty name matches the first
                          registry.
    - ``delete_on_destroy``: optional bool, default ``False``. When false,
                             ``delete()`` leaves the registry running. Ignored
                             when the ``adopted`` output is true.
* Outputs:
    - ``registry_name`` : the ctlptl ``Registry.name`` ultimately used
                          (autonamed, explicit, or adopted).
    - ``port``          : host port the registry is bound to (int).
    - ``env``           : registry container environment variables.
    - ``adopted``       : whether ``create()`` adopted an existing ctlptl
                          registry instead of creating one.
* Lifecycle:
    - ``check``  : pass-through validation only (deterministic, no random
                   generation). The default SDK behavior plus a port
                   carry-forward when the caller has unpinned a previously
                   bound port.
    - ``create`` : resolves the registry name (pinned value or freshly
                   minted ``<seed>-<hex>``), then ``ctlptl apply -f -``
                   with a Registry manifest that includes ``port`` only
                   if the caller pinned one (otherwise ctlptl picks), then
                   ``ctlptl get registry <name> -o json`` to harvest the
                   bound port.
    - ``delete`` : ``ctlptl delete registry <name>``.
    - ``diff``   : changing the pinned ``registry_name`` or pinned ``port``
                   forces replacement. Unpinned inputs (``None``) are
                   ignored — the user is saying "keep what's there."
    - ``read``   : verify the registry container still exists via
                   ``ctlptl get registry``; refresh the port from observed
                   state.

Pickling note
-------------
Pulumi serializes the provider class into stack state via cloudpickle. Keep
this module dependency-light (stdlib + ``pulumi`` only) and stable in module
path (``ctlptl.ctlptl_registry``) so the cloudpickled state round-trips.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
from typing import List, Optional

from pulumi import Input, Output, ResourceOptions
from pulumi.dynamic import (
    CheckResult,
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
    UpdateResult,
)

_REGISTRY_LISTEN_ADDRESS = "0.0.0.0"
_DEFAULT_REGISTRY_ENV = ["REGISTRY_PROXY_REMOTEURL=https://mirror.gcr.io"]
_DEFAULT_ADOPT_EXISTING = True
_DEFAULT_DELETE_ON_DESTROY = False


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
    result = subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command {cmd!r} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _registry_manifest(
    name: str,
    port: Optional[int] = None,
    env: Optional[List[str]] = None,
) -> str:
    """Build a Registry manifest, optionally pinning the host port.

    Omitting ``port`` (or passing 0) is equivalent to ``port: 0`` in the
    manifest and triggers ctlptl's ``freeport.GetFreePort()`` path. ctlptl's
    ``Apply`` also preserves an existing registry's port when the desired
    port is zero, so reapplying a portless manifest is idempotent.

    When ``port`` is a non-zero int, ctlptl binds the registry container to
    exactly that host port. Changing the value on a subsequent apply makes
    ctlptl recreate the container, which is why our ``diff`` forces a
    replace on port changes too.
    """
    body = (
        "apiVersion: ctlptl.dev/v1alpha1\n"
        "kind: Registry\n"
        f"name: {name}\n"
        f"listenAddress: {json.dumps(_REGISTRY_LISTEN_ADDRESS)}\n"
    )
    if port:
        body += f"port: {port}\n"
    if env:
        body += "env:\n"
        for value in env:
            body += f"- {json.dumps(value)}\n"
    return body


def _observe_port(name: str) -> int:
    """Return the host port ctlptl bound the registry to.

    ctlptl's ``Get`` populates both top-level ``port`` and ``status.hostPort``
    from the live Docker container. Prefer top-level; fall back to status.
    """
    result = _run(["ctlptl", "get", "registry", name, "-o", "json"])
    data = json.loads(result.stdout)
    port = data.get("port") or data.get("status", {}).get("hostPort")
    if not port:
        raise RuntimeError(
            f"ctlptl get registry {name!r} returned no port; raw JSON: {result.stdout!r}"
        )
    return int(port)


def _registry_name(data: dict) -> Optional[str]:
    """Return a registry object's name from ctlptl JSON."""
    name = data.get("name") or data.get("metadata", {}).get("name")
    return str(name) if name else None


def _registry_port(data: dict) -> Optional[int]:
    """Return a registry object's host port from ctlptl JSON."""
    port = data.get("port") or data.get("status", {}).get("hostPort")
    return int(port) if port else None


def _registry_env(data: dict) -> list[str]:
    """Return a registry object's live container environment."""
    return [
        str(item)
        for item in data.get("env") or data.get("status", {}).get("env") or []
    ]


def _env_is_compatible(data: dict, desired_env: Optional[list[str]]) -> bool:
    """Return whether an existing registry has the env we require."""
    if not desired_env:
        return True
    live_env = set(_registry_env(data))
    return all(item in live_env for item in desired_env)


def _first_registry_with_prefix(
    prefix: str,
    desired_env: Optional[list[str]],
    desired_port: Optional[int] = None,
) -> Optional[tuple[str, int]]:
    """Return the first existing registry whose name starts with *prefix*.

    An empty prefix intentionally matches the first registry in ctlptl's list.
    Registries with incompatible env are ignored so old retained registries do
    not silently disable pull-through cache mode.
    """
    result = _run(["ctlptl", "get", "registry", "-o", "json"], check=False)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    for item in data.get("items", []):
        name = _registry_name(item)
        if name is None or not name.startswith(prefix):
            continue
        if not _env_is_compatible(item, desired_env):
            continue
        port = _registry_port(item)
        if port is None:
            raise RuntimeError(
                f"ctlptl registry {name!r} has no observed port; "
                f"raw JSON: {json.dumps(item, sort_keys=True)!r}"
            )
        if desired_port is not None and port != desired_port:
            continue
        return name, port
    return None


def _bool_prop(props: dict, name: str, default: bool) -> bool:
    value = props.get(name)
    return default if value is None else bool(value)


def _env_prop(props: dict) -> Optional[List[str]]:
    value = props.get("env")
    if value is None:
        return list(_DEFAULT_REGISTRY_ENV)
    if not value:
        return None
    return [str(item) for item in value]


def _adopt_existing_prop(props: dict) -> bool:
    return _bool_prop(props, "adopt_existing", _DEFAULT_ADOPT_EXISTING)


def _delete_on_destroy_prop(props: dict) -> bool:
    return _bool_prop(props, "delete_on_destroy", _DEFAULT_DELETE_ON_DESTROY)


# ---------------------------------------------------------------------------
# Provider.
# ---------------------------------------------------------------------------


class _CtlptlRegistryProvider(ResourceProvider):
    """Lifecycle hooks for the CtlptlRegistry dynamic resource."""

    def check(self, olds: dict, news: dict) -> CheckResult:
        # Pure pass-through with one normalization: port carry-forward.
        # When the caller has unpinned a previously bound port (went from
        # ``port=N`` to ``port=None`` *or* ``port=0`` — both mean unpinned,
        # matching ctlptl's own ``desired.Port == 0`` convention), reuse
        # the bound value so ``diff`` doesn't see it as a meaningful change.
        #
        # We intentionally do NOT generate the random ``registry_name``
        # suffix here — that lives in ``create()`` so it runs exactly once
        # per resource lifetime. See the module docstring for why.
        news = dict(news)
        if not news.get("port") and olds.get("port"):
            news["port"] = int(olds["port"])
        return CheckResult(inputs=news, failures=[])

    def create(self, props: dict) -> CreateResult:
        _require_binary("ctlptl")
        adopt_existing = _adopt_existing_prop(props)
        env = _env_prop(props)
        if adopt_existing:
            prefix = props.get("registry_name") or ""
            desired_port = _registry_port({"port": props.get("port")})
            adopted = _first_registry_with_prefix(prefix, env, desired_port)
            if adopted is not None:
                registry_name, port = adopted
                return CreateResult(
                    id_=registry_name,
                    outs={
                        "registry_name": registry_name,
                        "port": port,
                        "env": env,
                        "adopt_existing": adopt_existing,
                        "adopted": True,
                        "delete_on_destroy": _delete_on_destroy_prop(props),
                    },
                )

        # Resolve the registry name: caller's pin, or autoname.
        registry_name: str = props.get("registry_name") or (
            f"{props.get('_autoname_seed') or 'ctlptl-registry'}-{secrets.token_hex(4)}"
        )
        _run(
            ["ctlptl", "apply", "-f", "-"],
            stdin=_registry_manifest(
                registry_name,
                props.get("port"),
                env,
            ),
        )
        return CreateResult(
            id_=registry_name,
            outs={
                "registry_name": registry_name,
                "port": _observe_port(registry_name),
                "env": env,
                "adopt_existing": adopt_existing,
                "adopted": False,
                "delete_on_destroy": _delete_on_destroy_prop(props),
            },
        )

    def delete(self, id_: str, props: dict) -> None:
        _require_binary("ctlptl")
        adopted = _bool_prop(props, "adopted", False)
        delete_on_destroy = _delete_on_destroy_prop(props)
        if adopted or not delete_on_destroy:
            reason = "adopted=True" if adopted else "delete_on_destroy=False"
            print(
                f"Leaving ctlptl registry {id_!r} in place because {reason}. "
                f"Delete it manually with: ctlptl delete registry {id_}",
                file=sys.stderr,
            )
            return
        _run(["ctlptl", "delete", "registry", id_], check=False)

    def diff(self, id_: str, olds: dict, news: dict) -> DiffResult:
        replaces: List[str] = []
        mutable_changes = False
        adopt_existing = _adopt_existing_prop(news)
        # ``registry_name``: only compare when the caller actually pinned
        # one. An unpinned ``news`` (``None``/empty) means "don't care,
        # keep what was minted by ``create()`` last time" — not a diff.
        pinned_name = news.get("registry_name")
        old_name = olds.get("registry_name") or id_
        if adopt_existing:
            if pinned_name and not str(old_name).startswith(str(pinned_name)):
                replaces.append("registry_name")
        elif pinned_name and pinned_name != old_name:
            replaces.append("registry_name")
        # Same semantic for ``port``: an unpinned ``news`` is "don't care".
        # Both ``None`` and ``0`` count as unpinned (matching ctlptl's
        # ``desired.Port == 0`` convention). ``check()`` already carried
        # the observed port forward when the caller went from pinned to
        # unpinned, so ``news.port`` will be the bound port in that case
        # and the comparison is a no-op.
        new_port = int(news.get("port") or 0)
        old_port = int(olds.get("port") or 0)
        if new_port and new_port != old_port:
            replaces.append("port")
        if _env_prop(news) != _env_prop(olds):
            replaces.append("env")
        if _adopt_existing_prop(news) != _adopt_existing_prop(olds):
            mutable_changes = True
        if _delete_on_destroy_prop(news) != _delete_on_destroy_prop(olds):
            mutable_changes = True
        # ``_autoname_seed`` only changes when the Pulumi logical name
        # changes, which alters the URN and is handled by the engine as a
        # separate resource (or via aliases), so we don't react to it.
        return DiffResult(
            changes=bool(replaces) or mutable_changes,
            replaces=replaces,
            delete_before_replace=True,
        )

    def update(self, id_: str, olds: dict, news: dict) -> UpdateResult:
        port = olds.get("port")
        try:
            port = _observe_port(id_)
        except Exception:
            pass
        outs = dict(news)
        outs["registry_name"] = id_
        if port:
            outs["port"] = int(port)
        outs["env"] = _env_prop(news)
        outs["adopt_existing"] = _adopt_existing_prop(news)
        outs["adopted"] = _bool_prop(olds, "adopted", False)
        outs["delete_on_destroy"] = _delete_on_destroy_prop(news)
        return UpdateResult(outs=outs)

    def read(self, id_: str, props: dict) -> ReadResult:
        _require_binary("ctlptl")
        result = _run(
            ["ctlptl", "get", "registry", id_, "-o", "json"],
            check=False,
        )
        if result.returncode != 0:
            # Registry was deleted out-of-band; signal "no longer exists".
            return ReadResult(id_=None, outs={})
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return ReadResult(id_=None, outs={})

        port = _registry_port(data)
        outs = dict(props)
        outs["registry_name"] = id_
        if port:
            outs["port"] = port
        outs["env"] = _env_prop(props)
        outs["adopt_existing"] = _adopt_existing_prop(props)
        outs["adopted"] = _bool_prop(props, "adopted", False)
        outs["delete_on_destroy"] = _delete_on_destroy_prop(props)
        return ReadResult(id_=id_, outs=outs)


# ---------------------------------------------------------------------------
# Resource class consumed by user code.
# ---------------------------------------------------------------------------


class CtlptlRegistry(Resource):
    """A local image registry managed via ctlptl.

    The ctlptl ``Registry.name`` is autonamed by default (Pulumi convention);
    pass ``registry_name="..."`` to pin it to an exact value. The host port
    is chosen by ctlptl by default; pass ``port=N`` (with N > 0) to pin it
    (changing the pinned value forces replacement). ``port=None`` and
    ``port=0`` are both treated as "unpinned", matching ctlptl's own
    manifest convention.

    The host-published registry port is always bound to ``0.0.0.0`` so Docker
    containers on other bridge networks can reach it via a host-routable
    address.

    By default, create uses get-or-create behavior: it adopts the first
    existing ctlptl registry whose name starts with ``registry_name`` if one
    exists, otherwise creates a new registry normally. If ``registry_name`` is
    empty or omitted, the first existing registry is adopted. The ``adopted``
    output records whether adoption actually happened. Default destroy behavior
    retains the registry. Set ``delete_on_destroy=True`` to delete registries
    Pulumi created; actually adopted registries are always retained.

    By default, the registry runs as a pull-through cache backed by
    ``mirror.gcr.io``.
    Set ``env`` to override the registry container environment.
    """

    registry_name: Output[str]
    port: Output[int]
    env: Output[List[str]]
    adopted: Output[bool]

    def __init__(
        self,
        name: str,
        registry_name: Optional[Input[str]] = None,
        port: Optional[Input[int]] = None,
        env: Optional[Input[List[Input[str]]]] = None,
        adopt_existing: Optional[Input[bool]] = None,
        delete_on_destroy: Optional[Input[bool]] = None,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            _CtlptlRegistryProvider(),
            name,
            {
                # Inputs:
                # ``registry_name`` may be None — ``create()`` autonames in
                # that case by combining ``_autoname_seed`` with a random
                # suffix. The value is then stored in state.
                "registry_name": registry_name,
                # ``port`` may be None or 0 (both mean "unpinned") — ctlptl
                # will pick a free port. When non-zero it's used verbatim
                # and changes force replacement. On subsequent runs
                # ``check()`` carries the observed port forward if the
                # caller has unpinned, so the bound port stays stable
                # across ``pulumi up`` cycles.
                "port": port,
                # Optional registry container environment. When omitted,
                # create() defaults to pull-through cache mode backed by
                # mirror.gcr.io.
                "env": env,
                # If true, create() first attaches to the first existing
                # ctlptl registry whose name starts with registry_name, then
                # falls back to normal creation when no match exists. An empty
                # registry_name intentionally adopts the first registry.
                # The adopted output records whether create() actually found
                # an existing registry or had to make a new one.
                "adopt_existing": adopt_existing,
                # If false, delete() removes only the Pulumi state handle and
                # prints manual cleanup instructions instead of deleting the
                # ctlptl registry. Ignored when adopt_existing is true.
                "delete_on_destroy": delete_on_destroy,
                # Hidden seed used by ``create()`` for autoname derivation.
                # Sourced from the Pulumi logical name so the autonamed value
                # is greppable (e.g. "registry-a1b2c3d4").
                "_autoname_seed": name,
            },
            opts,
        )
