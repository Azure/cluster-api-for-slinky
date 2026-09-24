# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import pytest
import pulumi
import pulumi_kubernetes as k8s

from stacks.workload_cluster.workload_cluster_deployments import (
    _cert_manager_values,
    _coredns_controller_placement_spec,
    _keda_release_name,
    _keda_scaled_object_name,
    _keda_scaled_object_spec,
    _keda_values,
    _prometheus_server_address,
    _prometheus_service_name,
    _prometheus_values,
    _SLINKY_CHART_VERSION,
    _slurm_operator_values,
    _slurm_bridge_token_spec,
    _slurm_bridge_toleration,
    _slurm_bridge_values,
    _slurm_nodeset_name,
    _slurm_nodeset_values,
    _slurm_values,
    SlinkyDeploymentConfig,
    SlinkyImageConfig,
    SlurmNodeSetSpec,
    WorkloadClusterDeployments,
)
from stacks.workload_cluster.workload_cluster_infrastructure import (
    controller_node_affinity,
    controller_pod_spec,
    controller_taint,
    controller_tolerations,
)


def test_controller_pool_uses_critical_addons_only_taint() -> None:
    assert controller_taint() == {
        "key": "CriticalAddonsOnly",
        "value": "true",
        "effect": "NoSchedule",
    }
    assert controller_tolerations() == [
        {
            "key": "CriticalAddonsOnly",
            "operator": "Exists",
            "effect": "NoSchedule",
        }
    ]


def test_prometheus_values_pin_components_to_controller_node() -> None:
    values = _prometheus_values()
    expected_affinity = controller_node_affinity()
    expected_tolerations = controller_tolerations()

    assert values["prometheus"]["prometheusSpec"] == {
        "serviceMonitorSelectorNilUsesHelmValues": False,
        "podMonitorSelectorNilUsesHelmValues": False,
        "affinity": expected_affinity,
        "tolerations": expected_tolerations,
    }
    assert values["alertmanager"]["alertmanagerSpec"] == {
        "affinity": expected_affinity,
        "tolerations": expected_tolerations,
    }
    assert values["prometheusOperator"]["affinity"] == expected_affinity
    assert values["prometheusOperator"]["tolerations"] == expected_tolerations
    assert values["grafana"] == {
        "affinity": expected_affinity,
        "tolerations": expected_tolerations,
    }
    assert values["kube-state-metrics"] == {
        "affinity": expected_affinity,
        "tolerations": expected_tolerations,
    }


def test_keda_names_are_instance_scoped() -> None:
    assert _keda_release_name("local") == "local-keda"
    assert _keda_scaled_object_name("local", "compute") == "local-compute-nodeset-scaler"


def test_keda_values_pin_components_to_controller_node() -> None:
    values = _keda_values()
    expected_tolerations = controller_tolerations()
    expected_placement = {
        "affinity": controller_node_affinity(),
        "tolerations": expected_tolerations,
    }

    assert values["affinity"] == controller_node_affinity()
    assert values["tolerations"] == expected_tolerations
    assert values["metricsServer"] == expected_placement
    assert values["webhooks"] == expected_placement


def test_cert_manager_values_pin_every_chart_pod_to_controller_node() -> None:
    values = _cert_manager_values()
    expected_placement = {
        "affinity": controller_node_affinity(),
        "tolerations": controller_tolerations(),
    }

    assert values["affinity"] == expected_placement["affinity"]
    assert values["tolerations"] == expected_placement["tolerations"]
    assert values["webhook"] == expected_placement
    assert values["cainjector"] == expected_placement
    assert values["startupapicheck"] == expected_placement


def test_slinky_deployment_config_preserves_published_defaults() -> None:
    config = SlinkyDeploymentConfig()

    assert config.chart("slurm-operator-crds") == (
        "oci://ghcr.io/slinkyproject/charts/slurm-operator-crds"
    )
    assert config.operator_crds_chart_version == _SLINKY_CHART_VERSION
    assert config.operator_chart_version == _SLINKY_CHART_VERSION
    assert config.slurm_chart_version == _SLINKY_CHART_VERSION
    assert config.chart_plain_http is False
    assert "image" not in _slurm_operator_values(config)["operator"]
    assert "image" not in _slurm_operator_values(config)["webhook"]


def test_slurm_operator_values_apply_custom_images_and_pull_secrets() -> None:
    config = SlinkyDeploymentConfig(
        chart_oci_prefix="oci://registry.example/charts/",
        operator_crds_chart_version="1.3.0-dev.1",
        operator_chart_version="1.3.0-dev.2",
        slurm_chart_version="1.3.0-dev.3",
        operator_image=SlinkyImageConfig(
            repository="registry.example/slurm-operator",
            tag="feature",
        ),
        webhook_image=SlinkyImageConfig(
            repository="registry.example/slurm-operator-webhook",
            digest="sha256:abc123",
        ),
        image_pull_secrets=("registry-credentials",),
    )

    assert config.chart("slurm-operator") == (
        "oci://registry.example/charts/slurm-operator"
    )
    assert _slurm_operator_values(config) == {
        "operator": {
            **controller_pod_spec(),
            "image": {
                "repository": "registry.example/slurm-operator",
                "tag": "feature",
            },
        },
        "webhook": {
            **controller_pod_spec(),
            "image": {
                "repository": "registry.example/slurm-operator-webhook",
                "digest": "sha256:abc123",
            },
        },
        "imagePullSecrets": [{"name": "registry-credentials"}],
    }


@pytest.mark.parametrize(
    "image",
    [
        {"repository": "registry.example/slurm-operator"},
        {
            "repository": "registry.example/slurm-operator",
            "tag": "feature",
            "digest": "sha256:abc123",
        },
    ],
)
def test_slinky_image_requires_exactly_one_version_selector(
    image: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="exactly one of tag or digest"):
        SlinkyImageConfig.model_validate(image)


def test_plain_http_requires_oci_chart_source() -> None:
    with pytest.raises(ValueError, match="oci://"):
        SlinkyDeploymentConfig(
            chart_oci_prefix="http://registry.example/charts",
            chart_plain_http=True,
        )


def test_coredns_patch_pins_to_controller_node() -> None:
    assert _coredns_controller_placement_spec() == {
        "template": {
            "spec": {
                "affinity": controller_node_affinity(),
                "tolerations": controller_tolerations(),
            }
        }
    }


def test_slurm_nodeset_values_pin_pods_to_initial_node() -> None:
    values = _slurm_nodeset_values(
        SlurmNodeSetSpec(name="compute", node_type="compute", replicas=1)
    )

    assert values["pinToNode"] is True
    assert values["oversubscribeNode"] is False
    assert values["slurmd"]["image"] == {
        "repository": "ghcr.io/slinkyproject/slurmd",
        "tag": "26.05-ubuntu24.04",
    }
    assert values["podSpec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"][0] == {
        "key": "slinky.slurm.net/node-type",
        "operator": "In",
        "values": ["compute"],
    }
    assert "podAntiAffinity" not in values["podSpec"]["affinity"]
    assert values["podSpec"]["tolerations"] == [_slurm_bridge_toleration()]


def test_slurm_bridge_uses_compute_partition_and_controller_placement() -> None:
    placement = controller_pod_spec()
    assert _slurm_bridge_values() == {
        "schedulerConfig": {"partition": "compute"},
        "sharedConfig": {"slurmJwtSecret": "slurm-bridge-token"},
        "admission": placement,
        "controllers": placement,
        "scheduler": placement,
    }


def test_slurm_bridge_token_uses_slurm_chart_jwt_key() -> None:
    assert _slurm_bridge_token_spec() == {
        "jwtKeyRef": {"name": "slurm-auth-jwt", "key": "jwt.key"},
        "secretRef": {"name": "slurm-bridge-token", "key": "auth-token"},
        "username": "slurm",
        "refresh": True,
        "lifetime": "8760h",
    }


@pytest.mark.parametrize("plain_http", [False, True])
def test_bridge_resources_and_readiness_in_both_chart_paths(plain_http: bool) -> None:
    resources = {}
    options = {}

    class DeploymentMocks(pulumi.runtime.Mocks):
        def new_resource(self, args):
            resources[args.name] = args
            outputs = dict(args.inputs)
            if args.typ == "kubernetes:helm.sh/v3:Release":
                outputs["status"] = {
                    "name": args.name,
                    "namespace": args.inputs["namespace"],
                    "version": args.inputs["version"],
                    "status": "deployed",
                }
            elif args.typ == "kubernetes:helm.sh/v4:Chart":
                outputs["resources"] = []
            return args.name, outputs

        def call(self, args):
            raise AssertionError(f"unexpected provider invoke: {args.token}")

    def record_options(args):
        options[args.name] = args.opts
        return None

    pulumi.runtime.set_mocks(DeploymentMocks())

    @pulumi.runtime.test
    def check():
        provider = k8s.Provider("workload", kubeconfig="{}")
        deployments = WorkloadClusterDeployments(
            "deployments",
            instance="test",
            slurm_node_sets=(SlurmNodeSetSpec(name="compute", node_type="compute", replicas=1),),
            slinky=SlinkyDeploymentConfig(
                chart_plain_http=plain_http,
                operator_image=SlinkyImageConfig(repository="registry.example/operator", tag="custom"),
            ),
            workload_provider=provider,
            opts=pulumi.ResourceOptions(transformations=[record_options]),
        )

        def verify(values):
            version, status, ready, ready_dependencies, token_dependencies, bridge_dependencies = values
            assert version == "1.2.2"
            assert status["name"] == "slurm-bridge"
            assert ready is True
            bridge = resources["slurm-bridge"]
            token = resources["slurm-bridge-token"]
            assert bridge.typ == "kubernetes:helm.sh/v3:Release"
            assert bridge.inputs["chart"] == "oci://ghcr.io/slinkyproject/charts/slurm-bridge"
            assert bridge.inputs["version"] == version
            assert bridge.inputs["namespace"] == token.inputs["metadata"]["namespace"] == "slurm"
            assert bridge.inputs["values"] == _slurm_bridge_values()
            assert token.typ == "kubernetes:slinky.slurm.net/v1beta1:Token"
            assert token.inputs["spec"] == _slurm_bridge_token_spec()
            assert token.inputs["metadata"]["annotations"]["pulumi.com/waitFor"] == "jsonpath={.status.issuedAt}"
            assert {urn.rsplit("::", 1)[-1] for urn in token_dependencies} == {"slurm", "slurm-operator"}
            assert {urn.rsplit("::", 1)[-1] for urn in bridge_dependencies} == {
                "slurm-bridge-namespace", "slurm-bridge-token", "slurm", "workload-cert-manager",
            }
            assert any(urn.endswith("::slurm-bridge") for urn in ready_dependencies)
            assert options["slurm-bridge"].provider is provider
            assert options["slurm-bridge-token"].provider is provider
            expected_type = "kubernetes:helm.sh/v4:Chart" if plain_http else "kubernetes:helm.sh/v3:Release"
            assert resources["slurm"].typ == expected_type
            assert resources["slurm"].inputs["values"]["nodesets"]["compute"]["podSpec"]["tolerations"] == [{
                "key": "slinky.slurm.net/managed-node",
                "operator": "Equal",
                "value": "slurm-bridge-scheduler",
                "effect": "NoExecute",
            }]
            assert resources["slurm-operator"].inputs["values"]["operator"]["image"] == {
                "repository": "registry.example/operator", "tag": "custom",
            }

        return pulumi.Output.all(
            deployments.slurm_bridge_chart_version,
            deployments.slurm_bridge_status,
            deployments.workload_cluster_ready,
            pulumi.Output.from_input(deployments.workload_cluster_ready.resources()).apply(
                lambda dependencies: pulumi.Output.all(*[resource.urn for resource in dependencies])
            ),
            pulumi.Output.all(*[resource.urn for resource in options["slurm-bridge-token"].depends_on]),
            pulumi.Output.all(*[resource.urn for resource in options["slurm-bridge"].depends_on]),
        ).apply(verify)

    check()


def test_slurm_values_use_container_compatible_cgroups() -> None:
    values = _slurm_values(())

    assert values["configFiles"]["cgroup.conf"] == (
        "CgroupPlugin=cgroup/v2\nIgnoreSystemd=yes\n"
    )


def test_slinky_chart_supports_nodeset_oversubscription_control() -> None:
    assert _SLINKY_CHART_VERSION == "1.2.1"


def test_prometheus_server_address_uses_helm_release_name() -> None:
    assert (
        _prometheus_service_name("prometheus-a9b5a75d")
        == "prometheus-a9b5a75d-kube-p-prometheus"
    )
    assert _prometheus_server_address("prometheus-a9b5a75d") == (
        "http://prometheus-a9b5a75d-kube-p-prometheus."
        "prometheus.svc.cluster.local:9090"
    )


def test_keda_scaled_object_targets_slurm_nodeset() -> None:
    assert _slurm_nodeset_name("slurm-87aab368", "compute") == (
        "slurm-87aab368-worker-compute"
    )

    spec = _keda_scaled_object_spec(
        node_set_name="slurm-87aab368-worker-compute",
        min_replicas=1,
        max_replicas=10,
        prometheus_server_address=(
            "http://prometheus-a9b5a75d-kube-p-prometheus."
            "prometheus.svc.cluster.local:9090"
        ),
    )

    assert spec == {
        "scaleTargetRef": {
            "apiVersion": "slinky.slurm.net/v1beta1",
            "kind": "NodeSet",
            "name": "slurm-87aab368-worker-compute",
        },
        "minReplicaCount": 1,
        "maxReplicaCount": 10,
        "triggers": [
            {
                "type": "prometheus",
                "metadata": {
                    "serverAddress": (
                        "http://prometheus-a9b5a75d-kube-p-prometheus."
                        "prometheus.svc.cluster.local:9090"
                    ),
                    "query": 'sum(slurm_partition_jobs_pending{partition="all"})',
                    "threshold": "1",
                    "activationThreshold": "1",
                    "unsafeSsl": "true",
                },
            }
        ],
    }