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

from __future__ import annotations

import pytest

from gitrepo import GiteaBuiltinRepository, GitOpsRepository  # noqa: F401


@pytest.mark.skip(reason="TODO: instantiate GiteaBuiltinRepository(kubeconfig=DUMMY_KUBECONFIG); assert it creates exactly one Namespace named ``gitea``, one Secret named ``gitea-credentials`` with username/password keys, and one helm.sh/v3:Release with the pinned chart version")
def test_creates_expected_child_resources() -> None:
    pass


@pytest.mark.skip(reason="TODO: assert the helm Release's ``values`` dict has persistence.enabled=true and size=2Gi (regression guard for the PVC bug we tracked down in May 2026)")
def test_helm_values_pin_pvc_2gi() -> None:
    pass


@pytest.mark.skip(reason="TODO: GiteaBuiltinRepository must expose all five GitOpsRepository contract Outputs (url, url_external, default_branch, ssh_private_key, ssh_known_hosts); assert getattr(repo, field) is not None for each")
def test_contract_outputs_all_populated() -> None:
    pass


@pytest.mark.skip(reason="TODO: instantiate two GiteaBuiltinRepository objects with different names in the same Pulumi program; assert their child resource names are namespaced (no collision on the ``gitea-credentials`` Secret)")
def test_multiple_instances_isolated() -> None:
    pass
