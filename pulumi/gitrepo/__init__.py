"""Provider-agnostic GitOps source package.

Re-exports dispatching :class:`GitOpsRepository` and :class:`GitOpsWebhook`
components. Concrete implementations live in their own modules and are loaded
by provider name.

Layout
------
This package lives at ``pulumi/gitrepo/`` — a sibling of the project
entrypoint ``__main__.py`` and of the :mod:`ctlptl` package. Pulumi
auto-adds the project root (``pulumi/``) to ``sys.path`` for both the
language host and every dynamic-Resource worker subprocess, so
``import gitrepo`` works everywhere without any editable install or
``sys.path`` shim.

TODO(multi-target): when adding cloud-hosted impls (GitHub, GitLab, ...),
each one lives in its own module here next to ``gitea_builtin.py`` and exposes
``<Provider>Repository`` / ``<Provider>Webhook`` classes. The shared contract
lives in :mod:`gitrepo._base` and is intentionally cloud-agnostic.
"""

from gitrepo._base import GitOpsConfig, GitOpsRepository, GitOpsWebhook

__all__ = ["GitOpsConfig", "GitOpsRepository", "GitOpsWebhook"]
