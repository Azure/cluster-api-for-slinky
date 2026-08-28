# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Pure Python local host discovery helpers."""

from __future__ import annotations

from functools import cache
import getpass


@cache
def discover_local_username() -> str | None:
    try:
        username = getpass.getuser()
    except OSError:
        return None
    return username or None
