"""Namespace shim for Pulumi dynamic-provider workers."""

from pathlib import Path

__path__ = [str(Path(__file__).resolve().parents[2])]
