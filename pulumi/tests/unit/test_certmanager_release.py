from stacks.control_plane.certmanager._release import (
    CERT_MANAGER_RELEASE_NAME,
    _cert_manager_values,
)


def test_cert_manager_uses_retry_stable_release_contract() -> None:
    assert CERT_MANAGER_RELEASE_NAME == "cert-manager"
    assert _cert_manager_values() == {
        "crds": {"enabled": True},
        "startupapicheck": {"enabled": False},
    }