"""Shared pytest fixtures for the umbrella Pulumi stack's test suite.

This file is intentionally minimal. Add fixtures here when they're useful
across BOTH ``tests/unit/`` and ``tests/integration/`` (e.g. a temp-dir
fixture that fakes a working git tree for ``GiteaSync`` tests). Fixtures
used by only one tier should live in that tier's ``conftest.py`` instead.

Pulumi's mock harness setup belongs in this file as well once we start
exercising the ``__main__`` program with ``pulumi.runtime.set_mocks(...)``
— see the TODO in tests/unit/test_gitops_factory.py.
"""

from __future__ import annotations

# Intentionally empty for now. Placeholder fixtures will land alongside
# the tests that need them.
