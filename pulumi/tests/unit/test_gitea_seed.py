"""Unit tests for :mod:`gitrepo.gitea_seed`.

The seed dynamic resource runs ``git push`` (subprocess) against a working
tree. Tests should:

* Use ``tmp_path`` to create a tiny fake git repo (``git init`` + one commit).
* Mock the network ``git push`` call with ``subprocess.run``.
* Skip the actual push; assert only the *intent* (URL, branch, ref).
"""

from __future__ import annotations

import pytest

from gitrepo.gitea_seed import GiteaSeed  # noqa: F401


@pytest.mark.skip(reason="TODO: fixture: tmp_path with `git init` + one commit; assert .create() invokes git with the expected ``push --force <url> HEAD:refs/heads/<branch>`` shape and surfaces the pushed HEAD sha in the output result")
def test_create_pushes_current_head() -> None:
    pass


@pytest.mark.skip(reason="TODO: stub git rev-parse HEAD to return a known sha; call .diff() with old=that-sha, new=same-sha; assert changes=[] (no rerun). Then bump local HEAD; assert .diff() flags ``source_head`` changed (rerun)")
def test_diff_reruns_only_when_local_head_moves() -> None:
    pass


@pytest.mark.skip(reason="TODO: assert .diff() flags ``url`` or ``default_branch`` change as replace=True \u2014 you can't ``git push --force`` to a different repo without first deleting state")
def test_diff_url_change_triggers_replace() -> None:
    pass


@pytest.mark.skip(reason="TODO: .delete() is documented as a no-op (deleting the seed shouldn't unpush commits); assert it returns cleanly and does NOT shell out to git")
def test_delete_is_noop() -> None:
    pass


@pytest.mark.skip(reason="TODO: simulate ``git push`` exit code 1 with ``failed to push some refs`` stderr; assert the provider surfaces a clear pulumi-visible error (not a stack trace)")
def test_create_surfaces_clean_error_on_push_failure() -> None:
    pass
