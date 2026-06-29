"""Shared pytest fixtures for the umbrella Pulumi stack's test suite.

This file is intentionally minimal. Add fixtures here when they're useful
across BOTH ``tests/unit/`` and ``tests/integration/`` (e.g. a temp-dir
fixture that fakes a working git tree for ``GitSync`` tests). Fixtures
used by only one tier should live in that tier's ``conftest.py`` instead.

Pulumi's mock harness setup belongs in this file as well once we start
exercising the ``__main__`` program with ``pulumi.runtime.set_mocks(...)``
— see the TODO in tests/unit/test_gitops_factory.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _ensure_event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Provide a current asyncio event loop for every unit test.

    Component-construction tests build ``pulumi.Output`` values outside a
    Pulumi runtime. ``pulumi.Output.from_input`` creates an
    ``asyncio.Future``, which requires a *current* event loop. Python 3.14
    removed the implicit loop creation that ``asyncio.get_event_loop()``
    used to perform, so those tests raise ``RuntimeError: There is no
    current event loop`` without this fixture. Setting (and closing) a
    fresh loop per test is a no-op for tests that never touch asyncio and
    is harmless on Python <= 3.13, where the loop was created implicitly.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        asyncio.set_event_loop(None)
        loop.close()
