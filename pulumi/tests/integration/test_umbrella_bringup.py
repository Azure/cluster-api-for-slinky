"""End-to-end integration tests for the umbrella stack.

These tests are SLOW (~2 min per up + ~30s per destroy), require a working
Docker daemon, and are excluded from the default pytest run via the
``integration`` marker (see ``pulumi/pyproject.toml``'s ``addopts``).

To run them locally::

    cd pulumi && pytest -m integration

They use the Pulumi Automation API to drive a real ``pulumi up`` against a
temp-dir backend, so they exercise the actual ``__main__.py`` program, the
real dynamic-Resource subprocesses, and the cloudpickle round-trip.
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
@pytest.mark.skip(reason="TODO: Automation API harness \u2014 create a LocalWorkspace pointing at this pulumi/ dir, init a temp ``local`` stack of project ``ca4s-infra`` on a file:// backend in tmp_path/.state, set PULUMI_CONFIG_PASSPHRASE='', call stack.up(), assert each of the 12 expected resources is in the result.summary, then stack.destroy() in a finally block")
def test_umbrella_up_creates_expected_resources() -> None:
    pass


@pytest.mark.integration
@pytest.mark.skip(reason="TODO: stack.up() twice back-to-back; second run must report ``unchanged=12`` (no resources replaced or updated) \u2014 this is the most important regression guard against accidentally non-idempotent providers")
def test_umbrella_second_up_is_noop() -> None:
    pass


@pytest.mark.integration
@pytest.mark.skip(reason="TODO: stack.up() then stack.refresh(); assert refresh reports no drift on the dynamic resources \u2014 their .read() methods must round-trip cleanly against the live container/registry/Gitea state they manage")
def test_umbrella_refresh_reports_no_drift() -> None:
    pass


@pytest.mark.integration
@pytest.mark.skip(reason="TODO: stack.up() then manually `docker rm -f ca4s-registry`; stack.refresh() must detect the missing container and the next stack.up() must heal it without operator intervention")
def test_umbrella_heals_external_deletion() -> None:
    pass
