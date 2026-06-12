"""Shared AWX objects owned by the control-plane layer."""

from __future__ import annotations

from collections.abc import Mapping
import json

import pulumi
import pulumi_awx as awx
from ca4s_flux_crds.source.v1 import GitRepository
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from ._provider import AWXProviderConfig, decode_secret_data_value


FLUX_GIT_IDENTITY_KEY = "identity"
AWX_ORGANIZATION_NAME = "ca4s"
AWX_SCM_CREDENTIAL_NAME = "ca4s-gitops-scm"
AWX_MANAGEMENT_KUBERNETES_CREDENTIAL_NAME = "ca4s-management-kubernetes"
AWX_INJECTABLE_KUBERNETES_CREDENTIAL_TYPE_NAME = "CA4S Kubernetes API Bearer Token"
AWX_INJECTABLE_KUBERNETES_CREDENTIAL_NAME = "ca4s-management-kubernetes-env"
AWX_DYNAMIC_INVENTORY_NAME = "ca4s-dynamic-inventory"
AWX_DYNAMIC_INVENTORY_SOURCE_NAME = "ca4s-capi-slurm"
AWX_CLUSTER_STATE_JOB_TEMPLATE_NAME = "ca4s-collect-cluster-state"
_SOURCE_CONTROL_CREDENTIAL_TYPE = "Source Control"
_KUBERNETES_CREDENTIAL_TYPE = "OpenShift or Kubernetes API Bearer Token"
_AWX_NAMESPACE = "awx"
_MANAGEMENT_READER_SERVICE_ACCOUNT = "awx-management-reader"
_MANAGEMENT_READER_TOKEN_SECRET = "awx-management-reader-token"
_KUBERNETES_API_ENDPOINT = "https://kubernetes.default.svc"
_SERVICE_ACCOUNT_TOKEN_KEY = "token"
_SERVICE_ACCOUNT_CA_KEY = "ca.crt"
_WAIT_FOR_SERVICE_ACCOUNT_TOKEN = "jsonpath={.data.token}"
_DYNAMIC_INVENTORY_PATH = "projects/awx/inventory/capi_slurm_inventory.py"
_CLUSTER_STATE_PLAYBOOK_PATH = "projects/awx/playbooks/collect_cluster_state.yml"
_CAPI_NAMESPACE = "default"
_NODE_TYPE_LABEL = "slinky.slurm.net/node-type"
_CONTROLLER_NODE_TYPE = "controller"
_COMPUTE_NODE_TYPE = "compute"


def _required_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Flux GitRepository {path} must be an object")
    return value


def _required_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Flux GitRepository {path} must be a non-empty string")
    return value


def flux_source_url(spec: object) -> str:
    return _required_string(_required_mapping(spec, "spec").get("url"), "spec.url")


def flux_source_branch(spec: object) -> str:
    ref = _required_mapping(_required_mapping(spec, "spec").get("ref"), "spec.ref")
    return _required_string(ref.get("branch"), "spec.ref.branch")


def flux_source_secret_name(spec: object) -> str:
    secret_ref = _required_mapping(
        _required_mapping(spec, "spec").get("secret_ref"),
        "spec.secretRef",
    )
    return _required_string(secret_ref.get("name"), "spec.secretRef.name")


def project_name_from_scm_url(scm_url: str) -> str:
    repo = scm_url.rstrip("/").rsplit("/", 1)[-1]
    return repo.removesuffix(".git") or "gitops"


def source_control_credential_inputs(*, ssh_key_data: str) -> str:
    return json.dumps(
        {"ssh_key_data": ssh_key_data},
        sort_keys=True,
    )


def management_kubernetes_credential_inputs(
    *,
    host: str,
    bearer_token: str,
    ssl_ca_cert: str,
) -> str:
    return json.dumps(
        {
            "bearer_token": bearer_token,
            "host": host,
            "ssl_ca_cert": ssl_ca_cert,
            "verify_ssl": True,
        },
        sort_keys=True,
    )


def injectable_kubernetes_credential_type_inputs() -> str:
    return json.dumps(
        {
            "fields": [
                {
                    "id": "host",
                    "label": "Kubernetes API endpoint",
                    "type": "string",
                },
                {
                    "id": "bearer_token",
                    "label": "Kubernetes API bearer token",
                    "secret": True,
                    "type": "string",
                },
                {
                    "default": True,
                    "id": "verify_ssl",
                    "label": "Verify SSL",
                    "type": "boolean",
                },
                {
                    "id": "ssl_ca_cert",
                    "label": "Kubernetes API certificate authority",
                    "multiline": True,
                    "secret": True,
                    "type": "string",
                },
            ],
            "required": ["host", "bearer_token"],
        },
        sort_keys=True,
    )


def injectable_kubernetes_credential_type_injectors() -> str:
    return json.dumps(
        {
            "env": {
                "CA4S_K8S_BEARER_TOKEN": "{{ bearer_token }}",
                "CA4S_K8S_HOST": "{{ host }}",
                "CA4S_K8S_SSL_CA_CERT": "{{ ssl_ca_cert }}",
                "CA4S_K8S_VERIFY_SSL": "{{ verify_ssl }}",
            }
        },
        sort_keys=True,
    )


def dynamic_inventory_variables() -> str:
    return json.dumps(
        {
            "capi_namespace": _CAPI_NAMESPACE,
            "compute_node_type": _COMPUTE_NODE_TYPE,
            "controller_node_type": _CONTROLLER_NODE_TYPE,
            "node_type_label": _NODE_TYPE_LABEL,
        },
        sort_keys=True,
    )


class AWXConfiguration(pulumi.ComponentResource):
    """Shared AWX organization, SCM credential, and GitOps project."""

    organization_id: Output[float]
    organization_name: Output[str]
    scm_credential_id: Output[float]
    scm_credential_name: Output[str]
    management_kubernetes_credential_id: Output[float]
    management_kubernetes_credential_name: Output[str]
    injectable_kubernetes_credential_id: Output[float]
    injectable_kubernetes_credential_name: Output[str]
    dynamic_inventory_id: Output[float]
    dynamic_inventory_name: Output[str]
    dynamic_inventory_source_id: Output[float]
    dynamic_inventory_source_name: Output[str]
    cluster_state_job_template_id: Output[float]
    cluster_state_job_template_name: Output[str]
    project_id: Output[float]
    project_name: Output[str]

    def __init__(
        self,
        name: str,
        *,
        provider_config: AWXProviderConfig,
        flux_source_namespace: pulumi.Input[str],
        flux_source_name: pulumi.Input[str],
        k8s_provider: k8s.Provider | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:control_plane:AWXConfiguration",
            name,
            props={},
            opts=opts,
        )

        awx_provider = provider_config.provider
        flux_source = GitRepository.get(
            f"{name}-flux-source",
            id=Output.concat(flux_source_namespace, "/", flux_source_name),
            opts=ResourceOptions(parent=self, provider=k8s_provider),
        )
        scm_url = flux_source.spec.apply(flux_source_url)
        scm_branch = flux_source.spec.apply(flux_source_branch)
        project_name = scm_url.apply(project_name_from_scm_url)
        flux_secret_name = flux_source.spec.apply(flux_source_secret_name)
        flux_secret = k8s.core.v1.Secret.get(
            f"{name}-flux-secret",
            id=Output.concat(flux_source_namespace, "/", flux_secret_name),
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[flux_source],
            ),
        )
        ssh_key_data = Output.secret(
            flux_secret.data.apply(
                lambda data: decode_secret_data_value(data, FLUX_GIT_IDENTITY_KEY)
            )
        )

        organization = awx.Organization(
            f"{name}-organization",
            name=AWX_ORGANIZATION_NAME,
            description="Cluster API for Slurm shared automation organization",
            opts=ResourceOptions(parent=self, provider=awx_provider),
        )

        source_control_type = awx.get_credential_type_output(
            name=_SOURCE_CONTROL_CREDENTIAL_TYPE,
            opts=pulumi.InvokeOutputOptions(
                provider=awx_provider,
                depends_on=[organization],
            ),
        )
        scm_credential_inputs = Output.secret(
            ssh_key_data.apply(
                lambda value: source_control_credential_inputs(ssh_key_data=value)
            )
        )
        scm_credential = awx.Credential(
            f"{name}-scm-credential",
            name=AWX_SCM_CREDENTIAL_NAME,
            description="Shared GitOps repository credential for AWX projects",
            credential_type=source_control_type.id,
            organization=organization.organization_id,
            inputs=scm_credential_inputs,
            opts=ResourceOptions(
                parent=self,
                provider=awx_provider,
                depends_on=[organization, flux_secret],
            ),
        )

        management_reader = k8s.core.v1.ServiceAccount(
            f"{name}-management-reader-sa",
            metadata={
                "name": _MANAGEMENT_READER_SERVICE_ACCOUNT,
                "namespace": _AWX_NAMESPACE,
            },
            opts=ResourceOptions(parent=self, provider=k8s_provider),
        )
        management_reader_role = k8s.rbac.v1.ClusterRole(
            f"{name}-management-reader-role",
            metadata={"name": _MANAGEMENT_READER_SERVICE_ACCOUNT},
            rules=[
                {
                    "api_groups": [""],
                    "resources": ["namespaces", "nodes", "pods", "services"],
                    "verbs": ["get", "list", "watch"],
                },
                {
                    "api_groups": ["cluster.x-k8s.io"],
                    "resources": [
                        "clusters",
                        "machinehealthchecks",
                        "machines",
                        "machinedeployments",
                        "machinepools",
                        "machinesets",
                    ],
                    "verbs": ["get", "list", "watch"],
                },
                {
                    "api_groups": ["controlplane.cluster.x-k8s.io"],
                    "resources": ["kubeadmcontrolplanes"],
                    "verbs": ["get", "list", "watch"],
                },
                {
                    "api_groups": ["bootstrap.cluster.x-k8s.io"],
                    "resources": ["kubeadmconfigs", "kubeadmconfigtemplates"],
                    "verbs": ["get", "list", "watch"],
                },
                {
                    "api_groups": ["infrastructure.cluster.x-k8s.io"],
                    "resources": [
                        "dockerclusters",
                        "dockerclustertemplates",
                        "dockermachines",
                        "dockermachinetemplates",
                    ],
                    "verbs": ["get", "list", "watch"],
                },
            ],
            opts=ResourceOptions(parent=self, provider=k8s_provider),
        )
        management_reader_binding = k8s.rbac.v1.ClusterRoleBinding(
            f"{name}-management-reader-binding",
            metadata={"name": _MANAGEMENT_READER_SERVICE_ACCOUNT},
            role_ref={
                "api_group": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": management_reader_role.metadata["name"],
            },
            subjects=[
                {
                    "kind": "ServiceAccount",
                    "name": management_reader.metadata["name"],
                    "namespace": _AWX_NAMESPACE,
                }
            ],
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[management_reader, management_reader_role],
            ),
        )
        management_reader_token = k8s.core.v1.Secret(
            f"{name}-management-reader-token",
            metadata={
                "name": _MANAGEMENT_READER_TOKEN_SECRET,
                "namespace": _AWX_NAMESPACE,
                "annotations": {
                    "kubernetes.io/service-account.name": (
                        _MANAGEMENT_READER_SERVICE_ACCOUNT
                    ),
                    "pulumi.com/waitFor": _WAIT_FOR_SERVICE_ACCOUNT_TOKEN,
                },
            },
            type="kubernetes.io/service-account-token",
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[management_reader],
                custom_timeouts=pulumi.CustomTimeouts(create="2m", update="2m"),
            ),
        )

        kubernetes_credential_type = awx.get_credential_type_output(
            name=_KUBERNETES_CREDENTIAL_TYPE,
            opts=pulumi.InvokeOutputOptions(
                provider=awx_provider,
                depends_on=[organization],
            ),
        )
        management_token = Output.secret(
            management_reader_token.data.apply(
                lambda data: decode_secret_data_value(data, _SERVICE_ACCOUNT_TOKEN_KEY)
            )
        )
        management_ca = Output.secret(
            management_reader_token.data.apply(
                lambda data: decode_secret_data_value(data, _SERVICE_ACCOUNT_CA_KEY)
            )
        )
        management_kubernetes_inputs = Output.secret(
            Output.all(management_token, management_ca).apply(
                lambda args: management_kubernetes_credential_inputs(
                    host=_KUBERNETES_API_ENDPOINT,
                    bearer_token=args[0],
                    ssl_ca_cert=args[1],
                )
            )
        )
        management_kubernetes_credential = awx.Credential(
            f"{name}-management-kubernetes-credential",
            name=AWX_MANAGEMENT_KUBERNETES_CREDENTIAL_NAME,
            description="Read-only management-cluster Kubernetes API credential",
            credential_type=kubernetes_credential_type.id,
            organization=organization.organization_id,
            inputs=management_kubernetes_inputs,
            opts=ResourceOptions(
                parent=self,
                provider=awx_provider,
                depends_on=[
                    organization,
                    management_reader_binding,
                    management_reader_token,
                ],
            ),
        )

        injectable_kubernetes_credential_type = awx.CredentialType(
            f"{name}-injectable-kubernetes-credential-type",
            name=AWX_INJECTABLE_KUBERNETES_CREDENTIAL_TYPE_NAME,
            description="Injectable Kubernetes API credential for CA4S AWX jobs",
            kind="cloud",
            inputs=injectable_kubernetes_credential_type_inputs(),
            injectors=injectable_kubernetes_credential_type_injectors(),
            opts=ResourceOptions(
                parent=self,
                provider=awx_provider,
                depends_on=[organization],
            ),
        )
        injectable_kubernetes_credential = awx.Credential(
            f"{name}-injectable-kubernetes-credential",
            name=AWX_INJECTABLE_KUBERNETES_CREDENTIAL_NAME,
            description=(
                "Read-only management-cluster Kubernetes API credential for "
                "AWX execution environments"
            ),
            credential_type=injectable_kubernetes_credential_type.credential_type_id,
            organization=organization.organization_id,
            inputs=management_kubernetes_inputs,
            opts=ResourceOptions(
                parent=self,
                provider=awx_provider,
                depends_on=[organization, injectable_kubernetes_credential_type],
            ),
        )

        project = awx.Project(
            f"{name}-project",
            name=project_name,
            description="Shared GitOps project containing AWX inventory and playbooks",
            organization=organization.organization_id,
            credential=scm_credential.credential_id,
            scm_type="git",
            scm_url=scm_url,
            scm_branch=scm_branch,
            scm_clean=True,
            scm_update_on_launch=True,
            # TODO: Re-enable once pulumi_awx/terraform-provider-awx reliably
            # parses ProjectUpdate wait responses. With this set, AWX synced
            # the project successfully but the provider failed the Pulumi
            # create while parsing the sync response, leaving an orphaned
            # Project outside Pulumi state.
            # wait_for_sync=True,
            timeout=300,
            opts=ResourceOptions(
                parent=self,
                provider=awx_provider,
                depends_on=[scm_credential],
            ),
        )

        dynamic_inventory = awx.Inventory(
            f"{name}-dynamic-inventory",
            name=AWX_DYNAMIC_INVENTORY_NAME,
            description="Dynamic inventory for CAPI/Slinky workload clusters",
            organization=organization.organization_id,
            variables=dynamic_inventory_variables(),
            opts=ResourceOptions(
                parent=self,
                provider=awx_provider,
                depends_on=[organization],
            ),
        )
        dynamic_inventory_source = awx.InventorySource(
            f"{name}-dynamic-inventory-source",
            name=AWX_DYNAMIC_INVENTORY_SOURCE_NAME,
            description="CAPI/Slinky dynamic inventory discovered from the management cluster",
            inventory=dynamic_inventory.inventory_id,
            credential=injectable_kubernetes_credential.credential_id,
            source="scm",
            source_project=project.project_id,
            source_path=_DYNAMIC_INVENTORY_PATH,
            source_vars=dynamic_inventory_variables(),
            overwrite=True,
            overwrite_vars=True,
            update_on_launch=True,
            timeout=300,
            opts=ResourceOptions(
                parent=self,
                provider=awx_provider,
                depends_on=[project, injectable_kubernetes_credential],
            ),
        )
        cluster_state_job_template = awx.JobTemplate(
            f"{name}-cluster-state-job-template",
            name=AWX_CLUSTER_STATE_JOB_TEMPLATE_NAME,
            description="Collect management-cluster CAPI state for Slinky workload clusters",
            inventory=dynamic_inventory.inventory_id,
            project=project.project_id,
            playbook=_CLUSTER_STATE_PLAYBOOK_PATH,
            job_type="run",
            ask_variables_on_launch=True,
            allow_simultaneous=True,
            timeout=300,
            opts=ResourceOptions(
                parent=self,
                provider=awx_provider,
                depends_on=[dynamic_inventory_source],
            ),
        )
        awx.JobTemplateAssociateCredential(
            f"{name}-cluster-state-kubernetes-credential",
            credential_id=injectable_kubernetes_credential.credential_id,
            job_template_id=cluster_state_job_template.job_template_id,
            opts=ResourceOptions(
                parent=self,
                provider=awx_provider,
                depends_on=[cluster_state_job_template, injectable_kubernetes_credential],
            ),
        )

        self.organization_id = organization.organization_id
        self.organization_name = organization.name
        self.scm_credential_id = scm_credential.credential_id
        self.scm_credential_name = scm_credential.name
        self.management_kubernetes_credential_id = (
            management_kubernetes_credential.credential_id
        )
        self.management_kubernetes_credential_name = management_kubernetes_credential.name
        self.injectable_kubernetes_credential_id = (
            injectable_kubernetes_credential.credential_id
        )
        self.injectable_kubernetes_credential_name = injectable_kubernetes_credential.name
        self.dynamic_inventory_id = dynamic_inventory.inventory_id
        self.dynamic_inventory_name = dynamic_inventory.name
        self.dynamic_inventory_source_id = dynamic_inventory_source.inventory_source_id
        self.dynamic_inventory_source_name = dynamic_inventory_source.name
        self.cluster_state_job_template_id = cluster_state_job_template.job_template_id
        self.cluster_state_job_template_name = cluster_state_job_template.name
        self.project_id = project.project_id
        self.project_name = project.name

        self.register_outputs(
            {
                "organization_id": self.organization_id,
                "organization_name": self.organization_name,
                "scm_credential_id": self.scm_credential_id,
                "scm_credential_name": self.scm_credential_name,
                "management_kubernetes_credential_id": (
                    self.management_kubernetes_credential_id
                ),
                "management_kubernetes_credential_name": (
                    self.management_kubernetes_credential_name
                ),
                "injectable_kubernetes_credential_id": (
                    self.injectable_kubernetes_credential_id
                ),
                "injectable_kubernetes_credential_name": (
                    self.injectable_kubernetes_credential_name
                ),
                "dynamic_inventory_id": self.dynamic_inventory_id,
                "dynamic_inventory_name": self.dynamic_inventory_name,
                "dynamic_inventory_source_id": self.dynamic_inventory_source_id,
                "dynamic_inventory_source_name": self.dynamic_inventory_source_name,
                "cluster_state_job_template_id": self.cluster_state_job_template_id,
                "cluster_state_job_template_name": self.cluster_state_job_template_name,
                "project_id": self.project_id,
                "project_name": self.project_name,
            }
        )