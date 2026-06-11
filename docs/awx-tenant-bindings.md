# AWX Tenant Binding Integration Plan

## Goal

Wire workload-cluster inventory into AWX tenant inventories and job templates
without mixing AWX concerns into workload-cluster class implementations.

The integration should let the PKO-owned init stack create these AWX objects:

- One tenant-level AWX inventory per logical tenant / Slurm deployment.
- Workload-cluster groups and variables attached to that tenant inventory.
- Job templates backed by the shared GitOps AWX project.
- Credential associations that let AWX jobs talk to the management Kubernetes
  API.

The first implementation should be small and provider-backed. It should not use
manual AWX API calls or hand-managed UI setup.

## Current State

The control-plane stack already owns tenant-agnostic AWX objects:

- `AWXOperator` installs the AWX Operator chart.
- `AWXInstance` creates the AWX custom resource.
- `AWXProviderConfig` builds an in-cluster AWX provider from the operator-created
  admin Secret.
- `AWXConfiguration` creates:
  - shared organization `ca4s`
  - shared SCM credential `ca4s-gitops-scm`
  - shared GitOps project derived from the Flux `GitRepository`
  - management-cluster Kubernetes credential `ca4s-management-kubernetes`

The workload layer already owns workload-cluster realization:

- `TenantsLocal` parses local workload-cluster config and fans out workload
  cluster instances.
- `LocalWorkloadClusterClass` creates the local CAPI/CAPD workload cluster,
  workload-cluster addons, Slurm, and readiness outputs.

The missing layer is a binding layer that consumes both sides.

## Design Rule

Keep ownership split by concern:

```text
ControlPlaneLocal
  owns AWX instance, AWX provider, shared AWX org/project/credentials

TenantsLocal
  owns tenant/workload-cluster inventory as data, not AWX resources

AWXTenants / AWXTenantBinding
  owns AWX tenant inventories, groups, and job templates
```

Do not make workload-cluster classes import AWX provider modules. Workload
classes should emit descriptors; AWX binding components should consume those
descriptors.

## Proposed Code Structure

```text
pulumi/stacks/control_plane/awx/
  _configuration.py          # existing shared org/project/credentials
  _context.py                # small AWXControlPlaneContext dataclass, or inline
  _tenant_binding.py         # new AWXTenants, AWXTenantBinding,
                             # AWXWorkloadClusterBinding

pulumi/stacks/workload_cluster/
  inventory.py               # new TenantInventory and WorkloadClusterInventory
                             # dataclasses / renderer helpers
  tenants_local.py           # expose tenant_inventories from workload outputs
  workload_cluster_local_local.py
                             # expose workload inventory descriptor outputs

pulumi/pko/_init_stack_local.py
  # compose ControlPlaneLocal + TenantsLocal + AWXTenants
```

The AWX binding code lives under `control_plane/awx` because it creates AWX API
resources. It depends only on neutral workload inventory descriptors, not on
workload implementation classes.

## Data Contracts

### AWX Control Plane Context

Expose a compact context from `AWXConfiguration` / `ControlPlaneLocal` so the
binding layer does not pass unrelated scalar outputs everywhere.

```python
@dataclass(frozen=True)
class AWXControlPlaneContext:
    provider: awx.Provider
    organization_id: pulumi.Output[float]
    project_id: pulumi.Output[float]
    project_name: pulumi.Output[str]
    management_kubernetes_credential_id: pulumi.Output[float]
```

`ControlPlaneLocal` can keep its current stack exports, but should also expose:

```python
self.awx_context = awx_configuration.context
```

### Workload Cluster Inventory

Add a neutral descriptor for one workload cluster shard:

```python
@dataclass(frozen=True)
class WorkloadClusterInventory:
    tenant_name: str
    instance: pulumi.Input[str]
    cluster_class: pulumi.Input[str]
    cluster_name: pulumi.Input[str]
    management_namespace: pulumi.Input[str]
    workload_kubeconfig_secret_name: pulumi.Input[str]
    workload_kubeconfig_secret_key: pulumi.Input[str]
    control_plane_name: pulumi.Input[str]
    worker_machine_deployments: Sequence[pulumi.Input[str]]
    ready: pulumi.Input[bool]
```

For the current local cluster, values are expected to look like:

```text
tenant_name: local
instance: local
cluster_class: local
cluster_name: local-workload
management_namespace: default
workload_kubeconfig_secret_name: local-workload-kubeconfig
workload_kubeconfig_secret_key: value
control_plane_name: local-control-plane
worker_machine_deployments: [local-head, local-compute]
```

### Tenant Inventory

Add a tenant aggregate descriptor:

```python
@dataclass(frozen=True)
class TenantInventory:
    name: str
    workload_clusters: Sequence[WorkloadClusterInventory]
```

The current local config is flat, so `TenantsLocal` can initially synthesize one
logical tenant named `local` that contains all configured workload clusters. A
future config shape can make tenants explicit:

```json
{
  "tenants": [
    {
      "name": "tenant-a",
      "workloadClusters": [
        {"name": "tenant-a-0", "class": "local"},
        {"name": "tenant-a-1", "class": "local"}
      ]
    }
  ]
}
```

The AWX binding layer should not care whether the tenant descriptor came from
the current flat config or a future explicit tenant config.

## AWX Binding Components

### `AWXTenants`

Aggregate component that creates one `AWXTenantBinding` per tenant.

```python
class AWXTenants(pulumi.ComponentResource):
    bindings: list[AWXTenantBinding]

    def __init__(
        self,
        name: str,
        *,
        awx_context: AWXControlPlaneContext,
        tenants: Sequence[TenantInventory],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None: ...
```

Responsibilities:

- Keep the init-stack composition tidy.
- Fan out tenant bindings.
- Export aggregate AWX IDs/names if useful.

### `AWXTenantBinding`

Creates tenant-level AWX resources.

```python
class AWXTenantBinding(pulumi.ComponentResource):
    inventory_id: pulumi.Output[float]
    inventory_name: pulumi.Output[str]
    job_template_ids: list[pulumi.Output[float]]
    workload_cluster_bindings: list[AWXWorkloadClusterBinding]
```

Initial resources:

- `awx.Inventory` named `ca4s-<tenant>`.
- Tenant variables describing the workload cluster shards.
- A synthetic `localhost` host with `ansible_connection=local` so management
  API jobs have an execution target before SSH/node inventory exists.
- One or more job templates backed by the shared AWX project.
- `JobTemplateAssociateCredential` to attach the management Kubernetes
  credential to those templates.

Initial inventory variables should be stable JSON:

```json
{
  "tenant_name": "local",
  "workload_clusters": [
    {
      "instance": "local",
      "cluster_class": "local",
      "cluster_name": "local-workload",
      "management_namespace": "default",
      "workload_kubeconfig_secret_name": "local-workload-kubeconfig",
      "worker_machine_deployments": ["local-head", "local-compute"]
    }
  ]
}
```

### `AWXWorkloadClusterBinding`

Creates cluster-shard AWX structure inside the tenant inventory.

```python
class AWXWorkloadClusterBinding(pulumi.ComponentResource):
    cluster_group_name: pulumi.Output[str]
    controller_group_name: pulumi.Output[str]
    compute_group_name: pulumi.Output[str]
```

Initial resources:

- `awx.Group` for the workload cluster, e.g. `cluster_local_workload`.
- `awx.Group` for controller nodes, e.g. `controller_local_workload`.
- `awx.Group` for compute nodes, e.g. `compute_local_workload`.
- Group variables containing CAPI cluster name, namespace, kubeconfig Secret
  reference, worker MachineDeployment names, and label conventions.

Use AWX groups before hosts because the first integration target is management
API introspection. Real node hosts can be added later from a dynamic inventory
source once the execution path is decided.

## Project and Job Template Integration

The global AWX project remains control-plane-owned in `AWXConfiguration`.
Tenant bindings reference it by `project_id`.

Initial job template:

```python
awx.JobTemplate(
    f"{tenant.name}-cluster-state",
    name=f"ca4s-{tenant.name}-cluster-state",
    inventory=inventory.inventory_id,
    project=awx_context.project_id,
    playbook="projects/awx/playbooks/collect_cluster_state.yml",
    job_type="run",
    ask_variables_on_launch=True,
    opts=pulumi.ResourceOptions(parent=self, provider=awx_context.provider),
)
```

Then attach the management-cluster Kubernetes credential:

```python
awx.JobTemplateAssociateCredential(
    f"{tenant.name}-cluster-state-management-k8s",
    job_template_id=cluster_state_template.job_template_id,
    credential_id=awx_context.management_kubernetes_credential_id,
    opts=pulumi.ResourceOptions(parent=self, provider=awx_context.provider),
)
```

The first playbook should run on `localhost` and query management-cluster
Kubernetes APIs. This validates the AWX/Kubernetes credential path without
requiring SSH to CAPD nodes.

Suggested new playbook path:

```text
projects/awx/playbooks/collect_cluster_state.yml
```

It should report, at minimum:

- CAPI `Cluster` status for each workload cluster.
- CAPI `Machine` status and addresses.
- `Node` list if the current RBAC allows it.

## Dynamic Inventory Source Path

After the provider-managed inventory/group/job-template path works, add a
project-backed inventory source for real host discovery:

```python
awx.InventorySource(
    f"{cluster.instance}-inventory-source",
    inventory=tenant_inventory.inventory_id,
    source="scm",
    source_project=awx_context.project_id,
    source_path="projects/awx/inventory/capi_slurm_inventory.py",
    source_vars=workload_cluster_inventory_source_vars(cluster),
    overwrite=True,
    overwrite_vars=True,
    update_on_launch=True,
    opts=pulumi.ResourceOptions(parent=self, provider=awx_context.provider),
)
```

Suggested inventory script path:

```text
projects/awx/inventory/capi_slurm_inventory.py
```

The inventory script should discover nodes from Kubernetes/CAPI, not from
Pulumi state. Inputs should come from inventory source variables:

- CAPI cluster name.
- Management namespace.
- Node type label, currently `slinky.slurm.net/node-type`.
- Controller node type, currently `controller`.
- Compute node type, currently `compute`.

Expected group shape:

```json
{
  "all": {"children": ["slurm", "cluster_local_workload"]},
  "slurm": {"children": ["controller", "compute"]},
  "cluster_local_workload": {
    "children": ["controller_local_workload", "compute_local_workload"]
  },
  "controller": {"children": ["controller_local_workload"]},
  "compute": {"children": ["compute_local_workload"]},
  "_meta": {"hostvars": {}}
}
```

This lets tenant-wide templates target `compute` or `slurm`, while shard-level
diagnostics can target `cluster_<name>`.

## Init Stack Wiring

`InitStackLocal` becomes the composition point:

```python
control_plane = ControlPlaneLocal(...)
tenants = TenantsLocal(
    "tenants-local",
    opts=pulumi.ResourceOptions(parent=self, depends_on=[control_plane]),
)
awx_tenants = AWXTenants(
    "awx-tenants",
    awx_context=control_plane.awx_context,
    tenants=tenants.tenant_inventories,
    opts=pulumi.ResourceOptions(parent=self, depends_on=[control_plane, tenants]),
)
```

Stack outputs can include:

```python
pulumi.export("awx_tenant_bindings", awx_tenants.bindings_output)
```

Keep `TenantsLocal.workload_clusters` as the existing operational output, and
add `TenantsLocal.tenant_inventories` as the AWX-oriented data contract.

## Dependency Graph

```text
AWXOperator
  -> AWXInstance
    -> AWXProviderConfig
      -> AWXConfiguration
        -> organization
        -> SCM credential
        -> shared project
        -> management Kubernetes credential
        -> AWXControlPlaneContext

TenantsLocal
  -> LocalWorkloadClusterClass[*]
    -> CAPI cluster / MachineDeployments
    -> workload kubeconfig Secret
    -> Slurm release
    -> WorkloadClusterInventory
  -> TenantInventory[*]

AWXControlPlaneContext + TenantInventory[*]
  -> AWXTenants
    -> AWXTenantBinding[*]
      -> awx.Inventory
      -> localhost awx.Host
      -> AWXWorkloadClusterBinding[*]
        -> awx.Group cluster/controller/compute groups
      -> awx.JobTemplate cluster-state
      -> awx.JobTemplateAssociateCredential management Kubernetes credential
```

## First Implementation Slice

Keep the first PR deliberately narrow:

1. Add descriptor dataclasses and rendering helpers.
2. Add `AWXControlPlaneContext` output from `AWXConfiguration` /
   `ControlPlaneLocal`.
3. Expose `tenant_inventories` from `TenantsLocal`.
4. Add `AWXTenants`, `AWXTenantBinding`, and `AWXWorkloadClusterBinding`.
5. Create one inventory, one localhost host, workload-cluster groups, and one
   management-cluster state job template for the local tenant.
6. Attach the existing `ca4s-management-kubernetes` credential to that job
   template.

Do not add SSH machine credentials, schedules, workflow templates, or AWX RBAC in
this slice.

## Follow-On Slices

1. Add the project-backed dynamic inventory script and `awx.InventorySource`.
2. Add a Slurm smoke job that uses Kubernetes exec into a login/controller pod.
3. Add SSH/node-host execution once there is a clean key-management story.
4. Add explicit tenant config with multiple workload-cluster shards.
5. Add AWX team/RBAC ownership boundaries.

## Tests

Unit tests should cover:

- Tenant/workload descriptor generation from the current local flat config.
- Stable inventory/group/job-template names.
- Stable JSON rendering for tenant inventory variables and group variables.
- AWX binding components creating the expected Pulumi resource graph under
  mocks.
- Dynamic inventory script JSON output once that script exists.

Runtime validation should cover:

1. PKO stack reaches `Ready=True`.
2. AWX tenant inventory exists.
3. Workload-cluster AWX groups exist.
4. Cluster-state job template exists and has the management Kubernetes
   credential associated.
5. Launching the job can call the management Kubernetes API and list the local
   workload cluster without printing credential material.

## Open Questions

- Should tenant naming initially be fixed to `local`, or should the current flat
  config get an optional `tenantName` field before AWX binding lands?
- Should the first cluster-state playbook use `kubernetes.core.k8s_info`, a small
  Python helper, or direct `kubectl` from the execution environment?
- Do we need a custom AWX execution environment with Kubernetes Python modules,
  or can the first job use tooling already present in the default AWX runner?
- Should inventory groups be Pulumi-managed permanently, or only a bootstrap
  until SCM inventory source owns all groups/hosts?
