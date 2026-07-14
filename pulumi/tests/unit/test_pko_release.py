from __future__ import annotations

from pko._release import _helm_values


def test_helm_values_size_controller_memory() -> None:
    values = _helm_values()

    assert values["resources"] == {
        "limits": {"cpu": "200m", "memory": "512Mi"},
        "requests": {"cpu": "200m", "memory": "512Mi"},
    }
    assert values["rbac"] == {
        "extraRules": [
            {
                "apiGroups": ["source.toolkit.fluxcd.io"],
                "resources": ["*"],
                "verbs": ["get", "list", "watch"],
            },
        ],
    }