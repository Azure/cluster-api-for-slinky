"""Unit tests for :mod:`ctlptl.ctlptl_registry`.

The provider wraps the ``ctlptl`` CLI plus a couple of ``docker`` shell-outs.
All five lifecycle methods are pure: they take dicts, shell out, and return
dicts. That means every test in this file should be a stdlib-only mock of
``subprocess.run`` \u2014 no Docker, no kind, no ``ctlptl`` binary required.
"""

from __future__ import annotations

import pytest

# Importing here (not inside the test functions) catches rename regressions
# at pytest *collection* time, which is when we want them. The provider
# class is module-private (``_CtlptlRegistryProvider``); tests that need it
# should reach through ``ctlptl.ctlptl_registry._CtlptlRegistryProvider``.
from ctlptl.ctlptl_registry import CtlptlRegistry  # noqa: F401


@pytest.mark.skip(reason="TODO: assert .check() rejects names with invalid chars (uppercase, slashes, length > 63)")
def test_check_rejects_invalid_registry_name() -> None:
    pass


@pytest.mark.skip(reason="TODO: stub subprocess.run for `ctlptl apply -f -`; assert the manifest YAML passed on stdin pins ``apiVersion: ctlptl.dev/v1alpha1`` and substitutes the chosen freeport into ``port:``")
def test_create_renders_manifest_with_freeport() -> None:
    pass


@pytest.mark.skip(reason="TODO: simulate a port-already-in-use stderr from ctlptl; assert .create() retries with a fresh freeport and only escalates after N retries")
def test_create_retries_on_port_collision() -> None:
    pass


@pytest.mark.skip(reason="TODO: assert .diff() flags ``host_port`` as replace=True (changing it requires a new container) and ``registry_name`` as replace=True (changes the docker container name)")
def test_diff_marks_replace_on_immutable_fields() -> None:
    pass


@pytest.mark.skip(reason="TODO: stub `docker inspect <registry_name>` to return a known container JSON; assert .read() round-trips id_ and surfaces the actual host port even if state's record drifted (drift detection)")
def test_read_detects_host_port_drift() -> None:
    pass


@pytest.mark.skip(reason="TODO: stub subprocess.run for `ctlptl delete registry <name>`; assert .delete() is idempotent when the registry is already gone (NotFound -> no-op, not error)")
def test_delete_idempotent_on_missing_registry() -> None:
    pass
