"""Runtime-only Kubernetes Secret helpers for dynamic resources.

These helpers intentionally use ``kubectl`` during dynamic-resource execution
instead of Pulumi ``Secret.get`` outputs. That keeps generated private key
material out of Pulumi resource inputs/outputs/state while still allowing the
host-side sync resources to consume Kubernetes-owned Secrets.
"""

from __future__ import annotations

import base64
import json
import subprocess


def read_secret_text(
    *,
    kubeconfig_path: str,
    namespace: str,
    name: str,
    key: str,
) -> str:
    result = subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            kubeconfig_path,
            "-n",
            namespace,
            "get",
            "secret",
            name,
            "-o",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout).get("data") or {}
    if key not in data:
        available = sorted(data.keys())
        raise RuntimeError(
            f"Secret {namespace}/{name} is missing key {key!r}; available keys: {available!r}"
        )
    return base64.b64decode(data[key]).decode("utf-8")


def secret_data(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")