# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Integration tests: slow, end-to-end, require Docker.

All tests in this directory must be decorated with
``@pytest.mark.integration`` so they're excluded from the default run
(see ``addopts`` in ``pulumi/pyproject.toml``).
"""
