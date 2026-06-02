"""Pulumi dynamic resource that uploads an SSH public key to Gitea.

The :class:`~gitrepo._base.GitOpsRepository` contract promises a ``url``
output pointing at an SSH endpoint and a source Secret holding the matching
private key. For PKO to authenticate against Gitea over SSH, the *public*
half of that keypair must be registered against the admin user inside Gitea — via
``POST /api/v1/user/keys`` (HTTP basic auth as the admin user). This
dynamic resource owns that lifecycle: create on first apply, delete
on tear-down, and adopt-by-title on conflict so partial-failure
recovery on the next ``pulumi up`` doesn't error out.

Idempotency on create
---------------------
Gitea returns ``422 Unprocessable Entity`` (not ``409 Conflict``) when
asked to register a public-key fingerprint that's already attached to
*any* user account. We match by ``title`` against the authenticated
user's existing keys and adopt the matching id if one exists; if a key
with a different title but the same fingerprint exists we surface the
422 error rather than silently adopting (different title = different
intent).

Why a dynamic resource rather than a one-shot ``command.local.Command``?
-----------------------------------------------------------------------
Same reasons as :class:`gitrepo.gitea_repo.GiteaRepo` — we want
create/delete/read/diff lifecycle hooks so ``pulumi destroy`` actually
removes the key from Gitea (otherwise a developer cycling through
keypairs across ``destroy``/``up`` would silently accumulate stale
public keys on the admin user's profile).
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


# Same readiness budget as ``gitea_repo``: the first call after a cold
# ``pulumi up`` can take ~30s while Gitea finishes admin-user creation.
_API_READY_ATTEMPTS = 60
_API_READY_DELAY_S = 2.0

# Shorter retry budget for read/delete: by the time we get here the
# server has typically been up for a while.
_API_QUICK_ATTEMPTS = 5
_API_QUICK_DELAY_S = 1.0


class _GiteaSSHKeyProvider(ResourceProvider):
    """Calls Gitea's REST API to create / delete a single SSH public key.

    Methods run inside a cloudpickled subprocess, so the ``requests``
    import is deliberately local to each method — keeps the provider
    serializable across the cloudpickle boundary regardless of which
    Pulumi venv happens to be loaded at pickle time.
    """

    def _session(self, props: dict):
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

    def _find_key_id_by_title(self, props: dict) -> Optional[int]:
        """Return the existing key id for ``title`` if one is attached.

        Walks the authenticated user's keys (one page is plenty — the
        admin user only ever has a handful of keys in this stack). Used
        both on 422 conflicts during ``create`` and during ``read``.
        """
        api_url = props["api_url"].rstrip("/")
        title = props["title"]
        s = self._session(props)
        r = s.get(f"{api_url}/api/v1/user/keys", timeout=10)
        r.raise_for_status()
        for entry in r.json():
            if entry.get("title") == title:
                return int(entry["id"])
        return None

    def create(self, props: dict) -> CreateResult:
        api_url = props["api_url"].rstrip("/")
        title = props["title"]
        public_key = props["public_key"].strip()

        self._wait_for_api(api_url, props)
        s = self._session(props)

        payload = {
            "title": title,
            "key": public_key,
            # ``read_only=False`` so this key has push rights — PKO
            # writes back state-summary commits via the Stack CR's
            # default behavior (none today, but leaving the door open).
            "read_only": False,
        }
        resp = s.post(f"{api_url}/api/v1/user/keys", json=payload, timeout=15)

        if resp.status_code == 201:
            data = resp.json()
            key_id = int(data["id"])
        elif resp.status_code == 422:
            # Could be either: (a) key fingerprint already attached to
            # a key with our title (resume), or (b) attached to a key
            # with a different title (we conflict with manual state).
            # Disambiguate by title — only adopt if our title matches.
            existing = self._find_key_id_by_title(props)
            if existing is None:
                raise RuntimeError(
                    f"Gitea SSH key create returned HTTP 422 but no key with "
                    f"title {title!r} exists; the key fingerprint is likely "
                    f"attached to a different user / title. Response: "
                    f"{resp.text[:500]}"
                )
            key_id = existing
        else:
            raise RuntimeError(
                f"Gitea SSH key create returned HTTP {resp.status_code}: "
                f"{resp.text[:500]}"
            )

        outs = {**props, "key_id": key_id}
        # ID uses ``title`` (not key_id) so a regeneration of the key
        # bytes with the same title is a regular update, not a replace
        # that drops Pulumi state mid-update. The diff hook below
        # decides when a replace is actually needed.
        return CreateResult(id_=title, outs=outs)

    def delete(self, _id: str, props: dict) -> None:
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
            # Gitea is gone (cluster destroyed). Nothing to clean up.
            return
        key_id = props.get("key_id")
        if key_id is None:
            # Pulumi held a partial state without the id; look it up.
            key_id = self._find_key_id_by_title(props)
            if key_id is None:
                return
        resp = s.delete(
            f"{api_url}/api/v1/user/keys/{key_id}", timeout=15
        )
        if resp.status_code not in (204, 404):
            raise RuntimeError(
                f"Gitea SSH key delete returned HTTP {resp.status_code}: "
                f"{resp.text[:500]}"
            )

    def diff(
        self,
        _id: str,
        old: dict,
        new: dict,
    ) -> DiffResult:
        # Title is the resource id — changing it is a rename, which
        # over the REST API is a delete + create. ``public_key`` rotation
        # is also a replace (Gitea has no in-place key update endpoint).
        # URL + credential drift is normal (LB IP shuffles, password
        # regen happens only on full destroy+up) and shouldn't trigger
        # a replace.
        replaces = []
        for key in ("title", "public_key"):
            if old.get(key) != new.get(key):
                replaces.append(key)
        return DiffResult(changes=bool(replaces), replaces=replaces or None)

    def read(self, id_: str, props: dict) -> ReadResult:
        api_url = props["api_url"].rstrip("/")
        try:
            self._wait_for_api(
                api_url,
                props,
                attempts=_API_QUICK_ATTEMPTS,
                delay=_API_QUICK_DELAY_S,
            )
            existing = self._find_key_id_by_title(props)
            if existing is not None:
                return ReadResult(
                    id_=id_, outs={**props, "key_id": existing}
                )
        except Exception:
            # Fall through to "return existing state". Pulumi will
            # detect drift on the next plan and offer to repair.
            pass
        return ReadResult(id_=id_, outs=props)


class GiteaSSHKey(Resource):
    """Public SSH key registered against a Gitea user.

    Outputs:

    * ``key_id`` — Gitea's numeric id for the registered key.
    """

    key_id: pulumi.Output[int]

    def __init__(
        self,
        name: str,
        *,
        api_url: pulumi.Input[str],
        admin_username: pulumi.Input[str],
        admin_password: pulumi.Input[str],
        title: pulumi.Input[str],
        public_key: pulumi.Input[str],
        opts: Optional[pulumi.ResourceOptions] = None,
    ) -> None:
        super().__init__(
            _GiteaSSHKeyProvider(),
            name,
            {
                "api_url": api_url,
                "admin_username": admin_username,
                "admin_password": admin_password,
                "title": title,
                "public_key": public_key,
                # Declared so Pulumi treats them as outputs the provider
                # populates rather than missing inputs.
                "key_id": None,
            },
            opts,
        )
