"""Provider-agnostic GitOps source package.

Re-exports the abstract :class:`GitOpsRepository` contract plus the concrete
implementations the umbrella stack's ``__main__.py`` can dispatch to. Keep
this list minimal: it's also the API surface other stacks would consume if
they ever import from us.

Layout
------
This package lives at ``pulumi/gitrepo/`` — a sibling of the umbrella
``__main__.py`` and of the :mod:`ctlptl` package. Pulumi auto-adds the
project root (``pulumi/``) to ``sys.path`` for both the language host and
every dynamic-Resource worker subprocess, so ``import gitrepo`` works
everywhere without any editable install or ``sys.path`` shim.

TODO(multi-target): when adding cloud-hosted impls (GitHub, GitLab,
CodeCommit), each one lives in its own module here next to
``gitea_builtin.py``. The umbrella dispatch on ``gitops_provider`` (see
``__main__.py``) gets one more ``elif`` per impl. The shared contract
(``url`` / ``url_external`` / ``default_branch`` / ``credentials_secret_*``)
lives in :mod:`gitrepo._base` and is intentionally cloud-agnostic.
"""

from gitrepo._base import GitOpsRepository
from gitrepo.gitea_builtin import GiteaBuiltinRepository

__all__ = ["GitOpsRepository", "GiteaBuiltinRepository"]
