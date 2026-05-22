"""Pulumi dynamic resource: a local image registry managed by `ctlptl`.

Design notes
------------
Models ctlptl's ``Registry`` kind as a first-class Pulumi resource. By
default ctlptl picks the host port itself when the manifest omits ``port``
(or sets it to ``0``).

Auto-naming
-----------
Following the Pulumi convention used by ``aws.s3.Bucket``, ``azure-native``
resources, etc., the underlying ``Registry.name`` is auto-generated unless
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
cluster manifest can reference the registry by its actual name (the
``${REGISTRY_NAME}`` placeholder in ``CtlptlCluster``'s vendored manifest).

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

* The resource is identified by the autonamed (or pinned) ``registry_name``.
* Inputs:
    - ``registry_name`` : optional explicit ctlptl ``Registry.name`` value.
                          When omitted, autoname kicks in.
    - ``port``          : optional host port to bind. ``None`` and ``0``
                          both mean "unpinned" — ctlptl picks a free port
                          and ``check()`` carries the observed value
                          forward on subsequent runs.
* Outputs:
    - ``registry_name`` : the ctlptl ``Registry.name`` ultimately used
                          (autonamed or explicit).
    - ``port``          : host port the registry is bound to (int).
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
from typing import List, Optional

from pulumi import Input, Output, ResourceOptions
from pulumi.dynamic import (
    CheckResult,
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
)


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
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        check=check,
    )


def _registry_manifest(name: str, port: Optional[int] = None) -> str:
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
    )
    if port:
        body += f"port: {port}\n"
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
        # Resolve the registry name: caller's pin, or autoname.
        registry_name: str = props.get("registry_name") or (
            f"{props.get('_autoname_seed') or 'ctlptl-registry'}-{secrets.token_hex(4)}"
        )
        desired_port: Optional[int] = props.get("port")
        _run(
            ["ctlptl", "apply", "-f", "-"],
            stdin=_registry_manifest(registry_name, desired_port),
        )
        port = _observe_port(registry_name)
        return CreateResult(
            id_=registry_name,
            outs={"registry_name": registry_name, "port": port},
        )

    def delete(self, id_: str, props: dict) -> None:
        _require_binary("ctlptl")
        _run(["ctlptl", "delete", "registry", id_], check=False)

    def diff(self, id_: str, olds: dict, news: dict) -> DiffResult:
        replaces: List[str] = []
        # ``registry_name``: only compare when the caller actually pinned
        # one. An unpinned ``news`` (``None``/empty) means "don't care,
        # keep what was minted by ``create()`` last time" — not a diff.
        pinned_name = news.get("registry_name")
        if pinned_name and pinned_name != olds.get("registry_name"):
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
        # ``_autoname_seed`` only changes when the Pulumi logical name
        # changes, which alters the URN and is handled by the engine as a
        # separate resource (or via aliases), so we don't react to it.
        return DiffResult(
            changes=bool(replaces),
            replaces=replaces,
            delete_before_replace=True,
        )

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

        port = data.get("port") or data.get("status", {}).get("hostPort")
        outs = dict(props)
        outs["registry_name"] = id_
        if port:
            outs["port"] = int(port)
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
    """

    registry_name: Output[str]
    port: Output[int]

    def __init__(
        self,
        name: str,
        registry_name: Optional[Input[str]] = None,
        port: Optional[Input[int]] = None,
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
                # Hidden seed used by ``create()`` for autoname derivation.
                # Sourced from the Pulumi logical name so the autonamed value
                # is greppable (e.g. "registry-a1b2c3d4").
                "_autoname_seed": name,
            },
            opts,
        )
