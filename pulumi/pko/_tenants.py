"""Per-env tenant fan-out, sibling building block of PKOBootstrap.

``Tenants`` is a thin dispatcher: it picks the right per-env concrete
implementation (``TenantsLocal`` etc.) based on the outer-stack env
moniker and instantiates it as a child. Each concrete impl owns its
own tenant inventory and emits one ``pulumi.com/v1`` Stack CR per
tenant pointing at the ``ca4s-workload-cluster`` inner project.

The dispatcher pattern here mirrors the per-env split the outer
``__main__.py`` already uses (``stack_local.py``, ``stack_<env>.py``),
just at the class level. Adding a new env is purely additive — drop
a new ``_tenants_<env>.py`` next to this file exposing a class named
``Tenants<Env>`` (``Env`` = ``env.capitalize()``); the dispatcher
resolves it by convention, no registry edit needed.

No mini-stack, no workspace pod, no extra reconcile loop — the
workload-cluster Stack CRs land directly in the management cluster
when PKOBootstrap runs.
"""

from __future__ import annotations

import importlib
from typing import Any

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from pko._stack_cr import StackCRSpec


# Per-env concrete impls live in ``pko._tenants_<env>`` and expose a
# class named ``Tenants<Env>`` (PascalCase). Resolved lazily on first
# ``Tenants(...)`` instantiation so a sibling module's import errors
# only surface when its env is the one being dispatched.
_TENANTS_MODULE_PREFIX = "pko._tenants_"


class Tenants(pulumi.ComponentResource):
    """Dispatch to the per-env concrete tenants impl.

    Instantiated by :class:`pko.pko_bootstrap.PKOBootstrap` as a
    sibling of ``StateBackend``, ``WorkspaceServiceAccount`` etc. —
    the outer stack itself never references this class directly.

    Args:
        name: Pulumi resource name; prefix for the concrete impl
            and its child Stack CRs.
        env: Outer-stack env moniker (``pulumi.get_stack()`` value).
            Must be a literal ``str`` because dispatch happens at
            construction time; no ``Output``/``Awaitable`` form.
        stack_spec: Shared :class:`StackCRSpec` from PKOBootstrap.
            Forwarded unchanged to the concrete impl.
        control_plane_stack: ``metadata.name`` of the control-plane
            Stack CR. Concrete impls thread it into each emitted
            Stack CR's ``spec.prerequisites``.
        config: Optional inline Pulumi config map written into emitted Stack
            CRs. Opaque pass-through; inner projects own key semantics.
        provider: Kubernetes provider for the management cluster.
        opts: Standard ``ResourceOptions``.

    Outputs:
        workload_cluster_stacks: List of every emitted Stack CR's
            ``metadata.name``, passed through from the concrete impl.
    """

    workload_cluster_stacks: list[Output[str]]

    def __init__(
        self,
        name: str,
        *,
        env: str,
        stack_spec: StackCRSpec,
        control_plane_stack: pulumi.Input[str],
        config: dict[str, Any] | None,
        provider: k8s.Provider,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:pko:Tenants", name, props={}, opts=opts)

        module_name = f"{_TENANTS_MODULE_PREFIX}{env}"
        class_name = f"Tenants{env.capitalize()}"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # Only rewrap a failure to find the dispatch module itself;
            # a missing transitive import inside ``_tenants_<env>.py``
            # should propagate as-is so the real cause stays visible.
            if exc.name != module_name:
                raise
            raise ValueError(
                f"no Tenants impl for env {env!r}: expected a sibling "
                f"module {module_name!r} next to _tenants.py exposing "
                f"a class {class_name!r}. Create "
                f"pulumi/pko/_tenants_{env}.py to register this env."
            ) from None
        try:
            concrete = getattr(module, class_name)
        except AttributeError as exc:
            raise ValueError(
                f"module {module_name!r} does not expose a class "
                f"{class_name!r}; per-env Tenants impls must follow the "
                f"``Tenants<Env>`` (Env = env.capitalize()) naming "
                "convention so the dispatcher can resolve them."
            ) from exc

        impl = concrete(
            f"{name}-impl",
            stack_spec=stack_spec,
            control_plane_stack=control_plane_stack,
            config=config,
            provider=provider,
            opts=ResourceOptions(parent=self),
        )

        # All concrete impls expose this list by convention; the
        # registry is heterogeneously typed so we read through the
        # ComponentResource base. Runtime contract enforced by the
        # convention in each concrete file.
        stacks: list[Output[str]] = impl.workload_cluster_stacks  # type: ignore[attr-defined]
        self.workload_cluster_stacks = stacks
        self.register_outputs(
            {"workload_cluster_stacks": stacks}
        )
