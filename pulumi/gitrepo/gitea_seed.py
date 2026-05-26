"""Pulumi dynamic resource: one-time ``git push`` from a local working tree.

After :class:`~gitrepo.gitea_repo.GiteaRepo` lands an empty repo
inside Gitea, this resource force-pushes the current ``HEAD`` of a local
git working tree to that repo's default branch — the "seed" that gives
PKO something to reconcile from on its first pass.

Why not just point PKO at GitHub / origin directly?
---------------------------------------------------
Two reasons this stack exists at all:

1. Air-gapped / offline-dev parity. The reconcile loop should not depend
   on external network availability once the cluster is up.
2. We want PKO to see exactly the working-tree state the developer is
   iterating on, not the latest push to ``origin``. Hand-editing the
   manifests, ``pulumi up``, and immediately seeing PKO pick the change
   up is the whole loop this enables.

When the seed re-pushes
-----------------------
The resource id is ``<owner>/<repo>@<head_sha>``. Pulumi diffs that
against the previous-run id; if ``head_sha`` has changed (i.e. the user
committed something new since the last ``pulumi up``), it triggers a
*replace*, which under the hood:

* Calls our ``delete`` — a no-op, because deleting the seed resource
  shouldn't un-push a commit. Repo destruction is GiteaRepo's job.
* Calls our ``create`` — force-pushes the new HEAD.

``--force`` is intentional. The seed is the single source of truth for
this repo; if someone hand-edits via the Gitea web UI, the next ``pulumi
up`` will clobber it. That's the contract — this repo is *generated*,
not *curated*.

Auth
----
HTTPS basic auth via username+password embedded in the push URL. We rely
on ``GiteaBuiltinRepository`` configuring the admin password to be
alphanumeric-only (``RandomPassword(special=False)``) so we never have
to URL-encode special characters in the password.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

import pulumi
from pulumi.dynamic import (
    CreateResult,
    DiffResult,
    Resource,
    ResourceProvider,
)


def _git_head_sha(source_dir: str) -> str:
    """Resolve ``HEAD`` of ``source_dir`` to a commit SHA.

    Pulled out so both the dynamic provider (which runs in a subprocess)
    and the parent ``GiteaSeed.__init__`` (which runs in the Pulumi
    program) can call it. Top-level for cloudpickle friendliness.
    """
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class _GiteaSeedProvider(ResourceProvider):
    """Force-pushes a local git tree to a Gitea repo's default branch.

    Both ``create`` and ``delete`` are designed to be idempotent in the
    sense that running them twice does not leave the world in a
    different state than running them once.
    """

    def _build_push_url(self, props: dict) -> str:
        """Construct the authenticated HTTPS clone URL.

        Embedding credentials in the URL is the simplest way to feed
        them to ``git push``; we deliberately don't go through a
        credential helper because this is a one-shot operation and the
        password is alphanumeric (no URL-escape hazards).

        The URL is only ever materialized in this process's memory and
        as an argv to a single subprocess — it does not get persisted
        in Pulumi state (state stores the raw user/password as separate
        properties; this composed URL is recomputed on each invocation).
        """
        api_url = props["api_url"].rstrip("/")
        user = props["admin_username"]
        pwd = props["admin_password"]
        owner = props["owner"]
        repo = props["repo_name"]
        scheme, _, rest = api_url.partition("://")
        return f"{scheme}://{user}:{pwd}@{rest}/{owner}/{repo}.git"

    def _push(self, props: dict) -> None:
        source_dir = props["source_dir"]
        branch = props["default_branch"]
        push_url = self._build_push_url(props)

        # ``GIT_TERMINAL_PROMPT=0`` makes git fail loudly instead of
        # blocking on a username/password TTY prompt if our embedded
        # credentials get rejected. Without it, an auth failure on a
        # non-interactive host hangs forever.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

        # ``HEAD:refs/heads/<branch>`` pushes whatever commit ``HEAD``
        # points at into the named *remote* branch, regardless of what
        # the local branch is called. That's important: the developer
        # might be on ``feat/whatever`` locally, but the GitOps repo
        # should always present a clean ``main`` (or whatever
        # ``default_branch`` is). ``--force`` because we *are* the
        # canonical source for this repo.
        subprocess.run(
            [
                "git",
                "push",
                "--force",
                push_url,
                f"HEAD:refs/heads/{branch}",
            ],
            cwd=source_dir,
            check=True,
            env=env,
            # Capture stderr so an auth failure shows up in Pulumi's
            # error log instead of vanishing into the void. stdout is
            # left to inherit (visible during ``pulumi up --verbose``).
            stderr=subprocess.PIPE,
            text=True,
        )

    def create(self, props: dict) -> CreateResult:
        # Recompute the head sha at push time too, in case the user
        # rebased / committed between Pulumi program execution and the
        # provider call. The id reflects whatever we actually pushed.
        head = _git_head_sha(props["source_dir"])
        # The id captures both repo identity *and* the commit at push
        # time. If the user later commits something new and re-runs
        # ``pulumi up``, the Pulumi program passes a new ``head_sha``
        # input, ``diff()`` returns ``replaces=['head_sha']``, and
        # Pulumi orchestrates delete+create to re-seed.
        try:
            self._push(props)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"git push to seed Gitea repo "
                f"{props['owner']}/{props['repo_name']} failed: "
                f"{(e.stderr or '').strip()[:500]}"
            ) from e
        return CreateResult(
            id_=f"{props['owner']}/{props['repo_name']}@{head}",
            outs={**props, "head_sha": head},
        )

    def diff(self, _id: str, old: dict, new: dict) -> DiffResult:
        # All of these should re-trigger a push:
        #   * owner / repo_name: pushing to a *different* repo
        #   * default_branch: target ref changed
        #   * source_dir: pushing from a different working tree
        #   * head_sha: same tree, new commit
        # api_url / credentials are intentionally NOT in this list —
        # those can shift without invalidating the existing seed.
        replaces = []
        for key in (
            "owner",
            "repo_name",
            "default_branch",
            "source_dir",
            "head_sha",
        ):
            if old.get(key) != new.get(key):
                replaces.append(key)
        return DiffResult(changes=bool(replaces), replaces=replaces or None)

    def delete(self, _id: str, _props: dict) -> None:
        # Intentional no-op. The seed is a verb (push), not a noun
        # (repo) — there's nothing to "un-push". When the user runs
        # ``pulumi destroy``, GiteaRepo is what tears down the
        # repository; this resource just goes away from state.
        return


class GiteaSeed(Resource):
    """Force-pushes ``source_dir``'s ``HEAD`` to ``<owner>/<repo>:<branch>``.

    Inputs are all required. ``head_sha`` is computed at construction
    time from ``source_dir`` (the local working tree) and surfaced both
    as an input (so Pulumi can detect "tree moved" and re-push) and as
    an output (so ``pulumi stack output`` can show what's actually in
    the remote).
    """

    head_sha: pulumi.Output[str]
    api_url: pulumi.Output[str]
    admin_username: pulumi.Output[str]
    admin_password: pulumi.Output[str]
    owner: pulumi.Output[str]
    repo_name: pulumi.Output[str]
    default_branch: pulumi.Output[str]
    source_dir: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        api_url: pulumi.Input[str],
        admin_username: pulumi.Input[str],
        admin_password: pulumi.Input[str],
        owner: pulumi.Input[str],
        repo_name: pulumi.Input[str],
        default_branch: pulumi.Input[str],
        source_dir: str,
        opts: Optional[pulumi.ResourceOptions] = None,
    ) -> None:
        # Compute HEAD synchronously here — ``source_dir`` is a local
        # path (plain ``str``, not ``Output``), so we can resolve it
        # without an ``apply``. The resulting ``head_sha`` is what
        # Pulumi diffs against on subsequent runs to decide whether
        # to re-push.
        head = _git_head_sha(source_dir)
        super().__init__(
            _GiteaSeedProvider(),
            name,
            {
                "api_url": api_url,
                "admin_username": admin_username,
                "admin_password": admin_password,
                "owner": owner,
                "repo_name": repo_name,
                "default_branch": default_branch,
                "source_dir": source_dir,
                "head_sha": head,
            },
            opts,
        )
