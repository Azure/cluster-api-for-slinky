"""Pulumi dynamic resource that materializes an empty Gitea repository.

The :class:`~gitrepo._base.GitOpsRepository` contract promises a ``url``
output pointing at a *real* git server. ``GiteaBuiltinRepository`` brings
the server up via Helm but Gitea won't auto-create repos on first push,
so we need an explicit REST API call to land an empty repository at
``<owner>/<name>``.

Why a dynamic resource rather than a one-shot ``command.local.Command``?
-----------------------------------------------------------------------
We want create / delete / read / diff lifecycle hooks so:

* ``pulumi destroy`` actually removes the repo from Gitea (the
  ``command.local`` resource has no built-in delete, only an explicit
  ``delete`` command that's annoying to share state with).
* Renaming ``owner`` / ``repo_name`` triggers a *replace* (delete +
  create) instead of silently leaving the old repo behind.
* Pulumi state mirrors actual repository identity, so the dependent
  :class:`~gitrepo.gitea_seed.GiteaSeed` resource gets a stable
  handle to push to.

Idempotency on create
---------------------
First-pass create returns ``201 Created``. If a partial failure on a
previous run left the repo behind, the API returns ``409 Conflict`` —
we treat that as "already there, adopt it" rather than erroring out.
That way you can recover from a Pulumi crash mid-way without manually
``DELETE``ing the repo first.

Auth model
----------
Basic auth against the Gitea admin user. The admin password is a
secret-typed Pulumi Output that flows through the dynamic resource's
``props``; Pulumi keeps it encrypted in state automatically.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import pulumi
from pulumi.dynamic import (
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
)


# How long to wait for a freshly-installed Gitea (LB IP just assigned)
# to start serving the API. Picked generously — the first call after a
# cold ``pulumi up`` of the whole stack can take ~30s while the Gitea
# pod finishes its first-boot admin user creation.
_API_READY_ATTEMPTS = 60
_API_READY_DELAY_S = 2.0

# Shorter retry budget for delete / read: by the time we get here the
# server has typically been up for a while, so a single hiccup is fine
# but we don't want to block ``pulumi destroy`` for a long time when
# the cluster has already been torn down.
_API_QUICK_ATTEMPTS = 5
_API_QUICK_DELAY_S = 1.0


class _GiteaRepoProvider(ResourceProvider):
    """Calls Gitea's REST API to create / delete a single repository.

    Methods on this class run inside a cloudpickled subprocess, so
    third-party imports (``requests``) are deliberately local to each
    method — putting them at module scope would force every consumer
    of the dynamic resource to have ``requests`` importable at
    pickle-resolution time, which gets fragile across venv boundaries.
    """

    def _session(self, props: dict):
        """Construct an authenticated ``requests`` session.

        Local import keeps this provider serializable across the
        cloudpickle boundary even if the calling venv doesn't have
        ``requests`` installed at pickle time.
        """
        import requests

        s = requests.Session()
        s.auth = (props["admin_username"], props["admin_password"])
        s.headers["Accept"] = "application/json"
        return s

    def _wait_for_api(
        self,
        api_url: str,
        props: dict,
        attempts: int = _API_READY_ATTEMPTS,
        delay: float = _API_READY_DELAY_S,
    ) -> None:
        """Block until ``GET /api/v1/version`` returns 2xx.

        We hit ``/version`` rather than ``/`` because the latter renders
        the (unauthenticated) homepage long before the admin user is
        created — ``/version`` requires the API to be fully online,
        which is the precondition we actually care about.
        """
        import requests

        s = self._session(props)
        last_err: Any = None
        for _ in range(attempts):
            try:
                r = s.get(f"{api_url}/api/v1/version", timeout=5)
                if r.ok:
                    return
                last_err = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                last_err = repr(e)
            time.sleep(delay)
        raise RuntimeError(
            f"Gitea at {api_url} not reachable after "
            f"{attempts * delay:.0f}s (last error: {last_err})"
        )

    def create(self, props: dict) -> CreateResult:
        api_url = props["api_url"].rstrip("/")
        owner = props["owner"]
        repo = props["repo_name"]
        branch = props["default_branch"]
        admin_user = props["admin_username"]

        self._wait_for_api(api_url, props)
        s = self._session(props)

        # Two endpoints. ``/user/repos`` creates under the authenticated
        # user; ``/admin/users/{owner}/repos`` creates on behalf of
        # someone else (requires admin token, which we have). Pick the
        # right one based on whether we're acting as the owner.
        if owner == admin_user:
            create_url = f"{api_url}/api/v1/user/repos"
        else:
            create_url = f"{api_url}/api/v1/admin/users/{owner}/repos"

        # ``auto_init=False`` because the very next thing that runs is
        # GiteaSeed, which force-pushes the local working tree. An
        # auto-init README would just be in the way (and create a
        # divergent history that the seed would have to clobber).
        # ``private=True`` because this repo is consumed only by PKO
        # inside the cluster — there's no anonymous reader to consider.
        payload = {
            "name": repo,
            "default_branch": branch,
            "auto_init": False,
            "private": True,
            "description": "Bootstrap GitOps repo managed by ca4s-infra",
        }
        resp = s.post(create_url, json=payload, timeout=15)

        if resp.status_code == 201:
            data = resp.json()
        elif resp.status_code == 409:
            # Already exists — adopt it. This is the "Pulumi crashed
            # mid-create, recover gracefully on next up" path.
            r = s.get(f"{api_url}/api/v1/repos/{owner}/{repo}", timeout=10)
            r.raise_for_status()
            data = r.json()
        else:
            raise RuntimeError(
                f"Gitea repo create returned HTTP {resp.status_code}: "
                f"{resp.text[:500]}"
            )

        outs = {
            **props,
            "repo_id": data.get("id"),
            "clone_url": data.get("clone_url"),
            "ssh_url": data.get("ssh_url"),
            "full_name": data.get("full_name"),
            "html_url": data.get("html_url"),
        }
        # ID uses ``owner/repo`` so it's stable across URL / credential
        # changes — those are tolerated by ``diff()`` below without
        # triggering a replace.
        return CreateResult(id_=f"{owner}/{repo}", outs=outs)

    def delete(self, id_: str, props: dict) -> None:
        api_url = props["api_url"].rstrip("/")
        s = self._session(props)
        try:
            self._wait_for_api(
                api_url,
                props,
                attempts=_API_QUICK_ATTEMPTS,
                delay=_API_QUICK_DELAY_S,
            )
        except RuntimeError:
            # The Gitea server is gone (cluster destroyed, chart
            # uninstalled, etc). Nothing to clean up server-side; let
            # Pulumi drop the resource from state.
            return
        resp = s.delete(f"{api_url}/api/v1/repos/{id_}", timeout=15)
        if resp.status_code not in (204, 404):
            raise RuntimeError(
                f"Gitea repo delete returned HTTP {resp.status_code}: "
                f"{resp.text[:500]}"
            )

    def diff(
        self,
        _id: str,
        old: dict,
        new: dict,
    ) -> DiffResult:
        # Only owner / repo_name changes warrant replacement; URL +
        # credential drift is normal (LB IP can shuffle on cluster
        # restart, password is regenerated only on explicit ``pulumi
        # destroy + up``) and shouldn't nuke the repo.
        replaces = []
        for key in ("owner", "repo_name"):
            if old.get(key) != new.get(key):
                replaces.append(key)
        return DiffResult(changes=bool(replaces), replaces=replaces or None)

    def read(self, id_: str, props: dict) -> ReadResult:
        api_url = props["api_url"].rstrip("/")
        s = self._session(props)
        try:
            self._wait_for_api(
                api_url,
                props,
                attempts=_API_QUICK_ATTEMPTS,
                delay=_API_QUICK_DELAY_S,
            )
            r = s.get(f"{api_url}/api/v1/repos/{id_}", timeout=10)
            if r.status_code == 200:
                data = r.json()
                outs = {
                    **props,
                    "repo_id": data.get("id"),
                    "clone_url": data.get("clone_url"),
                    "ssh_url": data.get("ssh_url"),
                    "full_name": data.get("full_name"),
                    "html_url": data.get("html_url"),
                }
                return ReadResult(id_=id_, outs=outs)
        except Exception:
            # Fall through to "return existing state". Pulumi will
            # detect drift on the next plan and offer to recreate.
            pass
        return ReadResult(id_=id_, outs=props)


class GiteaRepo(Resource):
    """Empty repository inside Gitea at ``<owner>/<repo_name>``.

    Outputs:

    * ``repo_id`` — Gitea's numeric repo id.
    * ``clone_url`` — HTTPS clone URL as Gitea reports it (uses whatever
      ``ROOT_URL`` the server was configured with, which may or may not
      be reachable from outside; prefer the ``url`` output from the
      parent component for in-cluster use).
    * ``ssh_url`` — SSH clone URL.
    * ``full_name`` — ``<owner>/<repo_name>``.
    * ``html_url`` — Web UI URL.
    """

    repo_id: pulumi.Output[int]
    clone_url: pulumi.Output[str]
    ssh_url: pulumi.Output[str]
    full_name: pulumi.Output[str]
    html_url: pulumi.Output[str]

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
        opts: Optional[pulumi.ResourceOptions] = None,
    ) -> None:
        super().__init__(
            _GiteaRepoProvider(),
            name,
            {
                "api_url": api_url,
                "admin_username": admin_username,
                "admin_password": admin_password,
                "owner": owner,
                "repo_name": repo_name,
                "default_branch": default_branch,
                # Declared as ``None`` so Pulumi knows these are outputs
                # the provider will populate; without them, the runtime
                # treats them as missing inputs and refuses to expose
                # them on the resource handle.
                "repo_id": None,
                "clone_url": None,
                "ssh_url": None,
                "full_name": None,
                "html_url": None,
            },
            opts,
        )
