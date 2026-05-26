"""Provider-agnostic ``GitOpsRepository`` contract.

This module defines the abstract shape every "git source for PKO" implementation
must produce. Concrete implementations live alongside it (see
``gitea_builtin.py``) and are selected by each ``stack_<name>.py`` target
module based on the ``ca4s-infra:gitops_provider`` config key.

Why an explicit contract?
-------------------------
PKO's ``Stack`` CRD is the only thing downstream consumers actually
care about — it has a small, well-defined input surface (URL, branch, optional
credentials Secret reference via ``spec.gitAuth``). Anything *upstream* of
that — built-in Gitea, externally-hosted Gitea, GitHub, GitLab, Bitbucket,
your own gitolite — is implementation detail. We model that contract here so
swapping providers is a one-line config change and the rest of the program
(PKO Stacks, seeding scripts, dashboards) doesn't care which one is in use.

Outputs that every concrete subclass must populate
--------------------------------------------------
* ``url``                          — In-cluster git URL the ``Stack.spec.projectRepo``
                                     will point at, e.g.
                                     ``http://gitea-http.gitea.svc.cluster.local:3000/owner/repo.git``.
                                     Always set; for external providers this is
                                     the same as ``url_external``.
* ``url_external``                 — Host-reachable git URL, suitable for the
                                     one-time hydration ``git push`` we do
                                     from the Pulumi runner. May coincide with
                                     ``url`` (external providers) or be a
                                     different scheme (in-cluster providers
                                     surface a port-forwarded ``localhost:N``).
* ``default_branch``               — Branch name the seed push targeted and
                                     ``Stack.spec.branch`` (or ``spec.commit``)
                                     should track. Conventionally ``main``.
* ``credentials_secret_name``      — Name of the credentials ``Secret`` (keys
                                     ``username`` + ``password``, or ``identity``
                                     + ``known_hosts`` for SSH) that
                                     ``Stack.spec.gitAuth.basicAuth.{userName,password}.name``
                                     (or the ssh equivalent) will reference.
* ``credentials_secret_namespace`` — Namespace the credentials Secret lives in.
                                     Conventionally the namespace PKO itself
                                     runs in for Stack-related plumbing
                                     (``pulumi-kubernetes-operator``).

Lifetime model
--------------
``GitOpsRepository`` is a Pulumi ``ComponentResource`` — concrete subclasses
declare child Pulumi resources (Helm releases, Secrets, custom dynamic
resources for one-time git-push seeding) parented to it. Tearing down a
``GitOpsRepository`` tears down the entire git server with it. Good for a
local dev loop; in shared environments you'd point at an external provider
that this stack only references rather than owns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pulumi

if TYPE_CHECKING:
    from pulumi import Output


# Single source of truth for the Pulumi resource type token used by the
# base class. Concrete subclasses must override this with their own
# specific token (e.g. ``ca4s:gitrepo:GiteaBuiltinRepository``) so the
# state file faithfully records which implementation produced the
# resource — handy for ``pulumi stack export`` audits and for upgrade
# paths that need to refuse to swap one impl for another.
_BASE_TYPE = "ca4s:gitrepo:GitOpsRepository"


class GitOpsRepository(pulumi.ComponentResource):
    """Abstract-ish base for a PKO-consumed git source.

    Subclasses MUST:
      1. Call ``super().__init__(name, t=<their own type token>, opts=opts)``.
      2. Populate every attribute declared in the ``Outputs`` section of the
         module docstring as a ``pulumi.Output`` (use ``Output.from_input(...)``
         for plain Python values).
      3. Call ``self.register_outputs({...})`` with the same dict so the values
         show up in ``pulumi stack output`` when the component is used at the
         top level.

    The base class itself doesn't construct child resources — it's a pure
    contract holder. We deliberately don't make it an ``abc.ABC`` because
    Pulumi's resource machinery introspects the class hierarchy, and adding
    an extra metaclass just to enforce abstractness isn't worth the friction.
    Type checkers will still flag a subclass that forgets to set an output,
    via the attribute declarations below.
    """

    url: "Output[str]"
    url_external: "Output[str]"
    default_branch: "Output[str]"
    credentials_secret_name: "Output[str]"
    credentials_secret_namespace: "Output[str]"

    def __init__(
        self,
        name: str,
        t: str = _BASE_TYPE,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(t, name, props={}, opts=opts)
