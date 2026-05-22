"""Unit tests for :mod:`gitrepo.gitea_repo`.

The provider talks to Gitea's REST API. Mock the HTTP layer with the
``responses`` library so tests can assert request payloads / headers /
basic-auth without a live Gitea pod.
"""

from __future__ import annotations

import pytest

from gitrepo.gitea_repo import GiteaRepo  # noqa: F401


@pytest.mark.skip(reason="TODO: responses.add(POST /api/v1/admin/users/<owner>/repos -> 201); assert request body has ``name``, ``private=true``, ``default_branch``, ``auto_init=false`` and uses HTTP basic auth with the admin password from props")
def test_create_posts_correct_payload() -> None:
    pass


@pytest.mark.skip(reason="TODO: responses.add returns 409 Conflict on first create; assert .create() treats the repo as adopted (returns success with the same id) rather than raising \u2014 this is the documented idempotency path for partial-failure recovery")
def test_create_adopts_existing_repo_on_409() -> None:
    pass


@pytest.mark.skip(reason="TODO: assert .diff() marks ``owner`` and ``repo_name`` as replace=True (renaming a repo via Gitea API is fragile; safer to recreate) while ``description`` is a mutable update")
def test_diff_owner_or_name_triggers_replace() -> None:
    pass


@pytest.mark.skip(reason="TODO: responses.add(GET /api/v1/repos/<owner>/<name> -> 200 with known JSON); assert .read() round-trips id_ and returns the live ``url`` / ``default_branch`` fields (drift detection if admin renamed via UI)")
def test_read_returns_live_url() -> None:
    pass


@pytest.mark.skip(reason="TODO: responses.add(DELETE -> 204) and responses.add(DELETE -> 404); assert both terminate cleanly. Idempotency on 404 is critical \u2014 ``pulumi destroy`` after a manual API delete must not error.")
def test_delete_idempotent_on_404() -> None:
    pass


@pytest.mark.skip(reason="TODO: simulate Gitea returning 503 (service starting); assert the provider's retry loop respects the timeout argument and gives up cleanly with a recognizable exception rather than hanging")
def test_create_respects_retry_timeout() -> None:
    pass
