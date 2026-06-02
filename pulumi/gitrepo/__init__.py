"""Provider-agnostic GitOps source package.

Re-exports the abstract :class:`GitOpsRepository` contract. Concrete
implementations live in their own modules and should be imported directly by
the ``stack_<name>.py`` target module that dispatches to them.

Layout
------
This package lives at ``pulumi/gitrepo/`` — a sibling of the project
entrypoint ``__main__.py`` and of the :mod:`ctlptl` package. Pulumi
auto-adds the project root (``pulumi/``) to ``sys.path`` for both the
language host and every dynamic-Resource worker subprocess, so
``import gitrepo`` works everywhere without any editable install or
``sys.path`` shim.

TODO(multi-target): when adding cloud-hosted impls (GitHub, GitLab, ...),
each one lives in its own module here next to ``gitea_builtin.py``. The
per-target stack module dispatches on the ``gitops_provider`` config key
(see ``stack_local.py``) with one more ``elif`` per impl. The shared
contract (``url`` / ``url_external`` / ``default_branch`` /
``credentials_secret_*``) lives in :mod:`gitrepo._base` and is
intentionally cloud-agnostic.
"""

from gitrepo._base import GitOpsRepository

__all__ = ["GitOpsRepository"]
