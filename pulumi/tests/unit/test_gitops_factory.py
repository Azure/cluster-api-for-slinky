"""Pulumi-mock-based tests for the ``gitops`` ComponentResource factory.

Unlike the dynamic-provider tests in the other modules of this directory,
these tests DO need Pulumi's runtime \u2014 specifically ``pulumi.runtime``'s
mock harness, which intercepts resource creation and lets us assert on the
DAG that ``GiteaBuiltinRepository.__init__`` builds.

Use this pattern (paraphrased from
https://www.pulumi.com/docs/iac/concepts/testing/unit/)::

    import pulumi
    import pulumi.runtime

    class _Mocks(pulumi.runtime.Mocks):
        def new_resource(self, args): return (args.name + "_id", args.inputs)
        def call(self, args): return {}

    pulumi.runtime.set_mocks(_Mocks(), preview=False)

The harness must be set BEFORE any ``pulumi.Resource`` constructor runs,
so a fixture in tests/unit/conftest.py would activate it for this file.
"""

import pytest
from cryptography.hazmat.primitives.serialization import load_ssh_private_key

from gitrepo import GitOpsRepository  # noqa: F401
from gitrepo.gitea_builtin import GiteaBuiltinRepository, _derive_ed25519_keypair  # noqa: F401


def test_ed25519_private_key_derivation_is_openssh_and_stable() -> None:
    keypair = _derive_ed25519_keypair("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    assert keypair["private"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert keypair["private"].endswith("-----END OPENSSH PRIVATE KEY-----\n")
    assert keypair["public"].startswith("ssh-ed25519 ")
    assert load_ssh_private_key(keypair["private"].encode("ascii"), password=None)
    assert keypair == _derive_ed25519_keypair(
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )


@pytest.mark.skip(reason="TODO: instantiate GiteaBuiltinRepository(kubeconfig=DUMMY_KUBECONFIG); assert it creates exactly one Namespace named ``gitea``, one Secret named ``gitea-credentials`` with username/password keys, and one helm.sh/v3:Release with the pinned chart version")
def test_creates_expected_child_resources() -> None:
    pass

@pytest.mark.skip(reason="TODO: instantiate two GiteaBuiltinRepository objects with different names in the same Pulumi program; assert their child resource names are namespaced (no collision on the ``gitea-credentials`` Secret)")
def test_multiple_instances_isolated() -> None:
    pass
