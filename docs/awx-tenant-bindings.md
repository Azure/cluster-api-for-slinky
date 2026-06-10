# AWX Tenant Inventory and Job Template Plan

## Goal

Add per-tenant AWX inventory and job template plumbing using the Terraform AWX provider bridged into Pulumi. This phase deliberately does not try to solve all global AWX configuration. It assumes AWX is already installed by the control-plane stack and focuses on binding each workload cluster / tenant into AWX as an operable target.

## Scope

Implement a per-tenant component that creates AWX objects for each workload cluster:

- Inventory for the workload cluster.
- Inventory source or generated inventory content for CAPI / Slurm nodes.
- Credentials needed by tenant jobs.
- Job templates bound to that inventory.
- Outputs that expose AWX object names/IDs for later workflows.

Out of scope for this phase:

- Multi-tenant AWX RBAC/team model.
- Self-service tenant access.
- Schedules and workflow templates.
- Custom execution environments.
- Full AWX global bootstrap beyond whatever minimal provider/config objects are required to create per-tenant resources.

## Current State

AWX is currently installed as management-plane infrastructure:

- `AWXOperator` installs the AWX Operator chart.
- `AWXInstance` creates the `AWX` custom resource.
- `ControlPlaneLocal` wires those into the local control-plane graph.

No AWX API resources are managed yet. The old manual README flow described creating SCM credentials, projects, inventories, inventory sources, machine credentials, and job templates through the AWX UI. This plan replaces that manual tenant-facing part with Pulumi-owned resources.

## Ownership Split

### Control Plane

The control-plane layer owns tenant-agnostic AWX readiness and shared inputs:

- AWX operator and instance.
- AWX API endpoint discovery.
- AWX admin credential lookup.
- Minimal AWX provider construction.
- Shared organization, if required by the provider resource model.
- Shared project, only if the provider can create reusable job templates that are later bound per tenant.

### Tenant Binding

The tenant binding layer owns per-workload-cluster AWX objects:

- Inventory named for the workload cluster / tenant.
- Inventory source or generated inventory script association.
- Credentials scoped to the workload cluster.
- Job templates bound to the inventory.
- Tenant-specific variables and labels.

## Proposed Components

### `AWXProviderConfig`

Location:

```text
pulumi/stacks/control_plane/awx/_provider.py
```

Responsibilities:

- Read the AWX admin password Secret created by the AWX Operator.
- Build the AWX API URL for in-cluster access:

```text
http://awx-service.awx.svc.cluster.local
```

- Wait until AWX API readiness succeeds before constructing dependent AWX API resources.
- Construct and expose the bridged AWX Pulumi provider.

Readiness check options:

- Prefer a small dynamic resource or command-like component that polls `/api/v2/ping/` with basic auth.
- Avoid using the external LoadBalancer URL for PKO-to-AWX traffic.
- Keep all AWX provider traffic inside the management cluster.

Outputs:

- `api_url`
- `admin_user`
- `admin_password`
- `provider`

### `AWXTenantBinding`

Location:

```text
pulumi/stacks/workload_cluster/awx_tenant_binding.py
```

or, if we want AWX-specific code colocated under control-plane APIs:

```text
pulumi/stacks/control_plane/awx/_tenant_binding.py
```

Preferred initial location: `pulumi/stacks/workload_cluster/awx_tenant_binding.py`, because the component is instantiated from tenant/workload descriptors and should not make workload-cluster classes import control-plane AWX modules directly.

Responsibilities:

- Accept one logical tenant descriptor plus one or more workload-cluster descriptors.
- Create the tenant-level AWX surface for the logical Slurm deployment.
- Instantiate one `AWXWorkloadClusterBinding` child per workload cluster shard.
- Expose tenant aggregate AWX IDs/names and child cluster binding outputs.

For the single-cluster local case, `AWXTenantBinding` will have exactly one child `AWXWorkloadClusterBinding`. For a future >5k-node tenant, `AWXTenantBinding` is the durable object representing the tenant's Slurm environment, while each workload cluster is a capacity shard attached to it.

Tenant-level ownership:

- Shared organization reference or tenant organization, depending on the eventual RBAC model.
- Shared project for playbooks and inventory scripts, usually pointing at the GitOps repository.
- SCM credential for that project, if not owned globally by `AWXConfiguration`.
- Tenant aggregate inventory, named for the logical Slurm tenant rather than one Kubernetes cluster.
- Tenant inventory variables that describe the logical Slurm deployment and the list of workload-cluster shards.
- Job templates that operate on the tenant as a whole, such as cluster-state collection, Slurm health checks, and future workflow entrypoints.
- Optional aggregate / constructed inventory if a single inventory with multiple inventory sources becomes too coarse.

Tenant-level resources should not own per-cluster kubeconfigs, per-cluster inventory source settings, or CAPI label selectors. Those belong to `AWXWorkloadClusterBinding`.

Inputs:

```python
@dataclass(frozen=True)
class AWXTenantDescriptor:
    tenant_name: str
    slurm_name: str
    slurm_namespace: str
    slurm_release_name: str
    project_name: str
    inventory_name: str
    workload_clusters: tuple[AWXWorkloadClusterDescriptor, ...]
```

Initial local values:

```text
tenant_name: local
slurm_name: local
slurm_namespace: slurm
slurm_release_name: slurm
project_name: cluster-api-provider-slinky
inventory_name: local
workload_clusters: (local-workload,)
```

Outputs:

- `inventory_id`
- `inventory_name`
- `project_id`
- `tenant_job_template_ids`
- `workload_cluster_bindings`

### `AWXWorkloadClusterBinding`

Location:

```text
pulumi/stacks/workload_cluster/awx_tenant_binding.py
```

or, if the file grows too large:

```text
pulumi/stacks/workload_cluster/awx_workload_cluster_binding.py
```

Responsibilities:

- Accept one workload-cluster descriptor and the tenant-level AWX inventory/project IDs.
- Attach that workload cluster to the tenant inventory as one inventory source.
- Create or reference credentials that are specific to that workload cluster.
- Emit child outputs that the tenant binding can aggregate.

Per-workload-cluster ownership:

- Inventory source name and `source_vars` for one CAPI/workload-cluster shard.
- Management-cluster CAPI selectors for that shard.
- Workload kubeconfig Secret reference for that shard.
- Kubernetes credential or credential input source for that shard, once the credential model is implemented.
- Optional per-cluster diagnostic job template with `limit` scoped to that cluster's inventory group.
- Cluster-specific inventory variables such as node label conventions, worker classes, capacity role, and Slurm participation role.

The inventory script should emit both aggregate groups and shard-scoped groups. For example, for cluster `local-workload`:

```json
{
  "all": {"children": ["slurm", "cluster_local_workload"]},
  "slurm": {"children": ["controller", "compute"]},
  "cluster_local_workload": {"children": ["controller_local_workload", "compute_local_workload"]},
  "controller": {"children": ["controller_local_workload"]},
  "compute": {"children": ["compute_local_workload"]}
}
```

This lets tenant-wide job templates target `compute` or `slurm`, while per-shard diagnostics can target `cluster_local_workload`.

Inputs:

```python
@dataclass(frozen=True)
class AWXWorkloadClusterDescriptor:
    tenant_name: str
    cluster_name: str
    cluster_class: str
    shard_name: str
    kubeconfig_secret_namespace: str
    kubeconfig_secret_name: str
    kubeconfig_secret_key: str
    slurm_namespace: str
    slurm_release_name: str
    slurm_role: str
    node_type_label: str
    controller_node_type: str
    compute_node_type: str
    worker_machine_deployments: tuple[str, ...]
```

Initial local values:

```text
tenant_name: local
cluster_name: local-workload
cluster_class: local
shard_name: local
kubeconfig_secret_namespace: default
kubeconfig_secret_name: local-workload-kubeconfig
kubeconfig_secret_key: value
slurm_namespace: slurm
slurm_release_name: slurm
slurm_role: primary
node_type_label: slinky.slurm.net/node-type
controller_node_type: controller
compute_node_type: compute
worker_machine_deployments: (local-head, local-compute)
```

Outputs:

- `inventory_source_id`
- `inventory_source_name`
- `kubernetes_credential_id` if managed by AWX
- `cluster_group_name`
- `controller_group_name`
- `compute_group_name`
- `diagnostic_job_template_id` if created

## AWX Resources to Create

Exact resource names depend on the bridged Terraform provider schema, but conceptually:

### Organization

Use an existing/shared organization if global config already created it. For this phase, if the AWX provider requires an organization for inventories/templates and no global org exists, create a minimal shared one:

```text
ca4s
```

This is logically control-plane-owned, but may be temporarily created by the first tenant binding until `AWXConfiguration` exists.

### Project

Create or reference a project pointing at the GitOps repository.

Options:

1. Shared project, control-plane-owned:
   - Name: `cluster-api-provider-slinky`
   - SCM URL: in-cluster or AWX-reachable GitOps URL.

2. Per-tenant project:
   - Not needed initially unless tenant repos diverge.

Recommendation: shared project.

### Inventory

Create one aggregate inventory per logical tenant / Slurm deployment:

```text
local
```

Inventory variables should include tenant context:

```yaml
tenant_name: local
slurm_name: local
slurm_namespace: slurm
slurm_release_name: slurm-...
node_type_label: slinky.slurm.net/node-type
controller_node_type: controller
compute_node_type: compute
workload_clusters:
  - shard_name: local
    capi_cluster_name: local-workload
    kubeconfig_secret_namespace: default
    kubeconfig_secret_name: local-workload-kubeconfig
```

For a future tenant split across multiple workload clusters, the inventory remains tenant-level and the workload-cluster list grows. Tenant-wide job templates should bind to this aggregate inventory.

### Inventory Source

Initial approach:

- Use a project-backed inventory source pointing at a dynamic inventory script committed in the repo.
- Replace the old `projects/test/roles/sync/files/run.py` assumptions.
- Create one inventory source per workload cluster shard, all attached to the tenant aggregate inventory.

New script should discover current CAPI topology by labels and ownership, not MachinePool:

- Head/controller group: Machines or Nodes with `slinky.slurm.net/node-type=controller`.
- Compute group: Machines or Nodes with `slinky.slurm.net/node-type=compute`.
- Filter by `cluster.x-k8s.io/cluster-name=<cluster_name>`.

Inventory source configuration:

```text
source: scm
source_project: cluster-api-provider-slinky
source_path: projects/awx/inventory/capi_slurm_inventory.py
source_vars: shard_name, cluster_name, node labels, Slurm role
update_on_launch: true
overwrite: true
overwrite_vars: true
```

If AWX inventory-source merging becomes too limiting at large scale, switch the aggregate layer to a constructed inventory: each `AWXWorkloadClusterBinding` creates a per-cluster inventory, and `AWXTenantBinding` creates the constructed inventory and tenant job templates against that aggregate. The component boundary stays the same.

### Credentials

#### SCM Credential

Credential for AWX to clone the GitOps repo.

Initial local option:

- Use Gitea username/password from the `gitea-credentials` Secret.
- This is easy because Gitea is local and already has an admin credential.

Better future option:

- Use a deploy key / SSH private key Secret, matching the Flux/GitOps credential model.

#### Kubernetes Credential

Credential for inventory and job templates to query management-cluster CAPI objects.

Initial option:

- Use the PKO / management-cluster service account token model only if AWX can safely consume a scoped token.
- Create a dedicated `awx-runner` ServiceAccount with read-only CAPI permissions plus any needed pod/log permissions.
- Store the kubeconfig/token/CA as an AWX Kubernetes credential.

Permissions should start read-only:

- `clusters.cluster.x-k8s.io`
- `machines.cluster.x-k8s.io`
- `machinedeployments.cluster.x-k8s.io`
- `machinesets.cluster.x-k8s.io`
- `dockerclusters.infrastructure.cluster.x-k8s.io`
- `nodes` in the workload cluster only if the inventory script queries workload nodes directly.

#### Machine Credential

Only needed for SSH-based jobs.

Initial local options:

- Reuse a configured private key if available through Pulumi config.
- Or skip SSH templates until we have a managed key story.

Recommendation for first implementation:

- Create Kubernetes and SCM credentials first.
- Add machine credential only if we implement a job template that actually SSHes into CAPD nodes.

## Job Templates

Create job templates bound to the per-tenant inventory.

### `sync-inventory`

Purpose:

- Force inventory source update from CAPI/Slurm state.

If the AWX provider has an inventory source update resource, prefer that. Otherwise, create the inventory source with `update_on_launch=true` and let downstream job templates refresh it.

### `slurm-smoke`

Purpose:

- Verify tenant Slurm responds and compute nodes are visible.

Possible playbook:

```text
projects/awx/playbooks/slurm_smoke.yml
```

Checks:

- `sinfo`
- `squeue`
- optional `srun hostname`

This requires either:

- SSH credential to login node / compute nodes, or
- Kubernetes credential to exec into the login pod.

Recommendation: prefer Kubernetes exec into the login pod first; it avoids managing SSH keys in AWX.

### `collect-cluster-state`

Purpose:

- Gather CAPI Machine status, Kubernetes node status, Slurm NodeSet status.

This can use the Kubernetes credential and does not require SSH.

## Inventory Script Refactor

Create a new inventory script instead of evolving the old test script in place:

```text
projects/awx/inventory/capi_slurm_inventory.py
```

Requirements:

- No `/runner/.kube/config` mount assumption.
- Read kubeconfig/token from AWX credential-provided environment or file path.
- Accept cluster name and label selectors from inventory variables or environment.
- Support current topology with `MachineDeployment`, not `MachinePool`.
- Group hosts by node type:

```json
{
  "all": {"children": ["controller", "compute"]},
  "controller": {"hosts": [...]},
  "compute": {"hosts": [...]},
  "_meta": {"hostvars": {...}}
}
```

For local CAPD, hostvars can use CAPI Machine addresses:

- Prefer `ExternalIP` if present.
- Fallback to `InternalIP`.
- Hostname from `status.addresses[type=Hostname]`.

## Wiring in Pulumi

### Step 1: Bridge AWX provider

- Add provider SDK under `pulumi/sdks/awx`.
- Add it to `pulumi/stacks/init/requirements.txt`.
- Verify import works inside PKO workspace.

### Step 2: Add AWX API readiness/provider component

- Add `AWXProviderConfig` or `AWXAPI` component under `control_plane/awx`.
- Export provider and readiness output.
- Wire it in `ControlPlaneLocal` after `AWXInstance`.

### Step 3: Add workload-cluster descriptor output

In `LocalWorkloadClusterClass`, output an `AWXWorkloadClusterDescriptor` with:

- tenant/workload/shard identity
- CAPI cluster name
- kubeconfig Secret ref
- Slurm namespace/release once known
- node label conventions
- worker MachineDeployment names

### Step 4: Add tenant binding component

Instantiate `AWXTenantBinding` once per tenant after:

- AWX provider config is ready.
- The tenant's workload cluster descriptors exist.
- Slurm release exists, if job templates reference Slurm objects.

`AWXTenantBinding` then instantiates one `AWXWorkloadClusterBinding` per descriptor. The local default is one tenant with one workload cluster; the future >5k-node shape is one tenant with multiple workload cluster descriptors.

This may require threading a control-plane AWX provider output into `TenantsLocal`, or introducing a coordinator component in `InitStackLocal` that receives both:

- `ControlPlaneLocal.awx_api`
- `TenantsLocal.workload_cluster_descriptors`

Recommendation: avoid making workload cluster classes import control-plane AWX modules directly. Use descriptors and a separate binding component to keep the boundary explicit.

## Dependency Graph

```text
AWXOperator
  -> AWXInstance
    -> AWXProviderConfig

LocalWorkloadClusterClass
  -> CAPI Cluster / MachineDeployments
  -> Slurm release
  -> AWXWorkloadClusterDescriptor output

AWXProviderConfig + AWXTenantDescriptor(workload cluster descriptors)
  -> AWXTenantBinding
    -> Inventory
    -> Project / shared credentials if not global
    -> Tenant job templates
    -> AWXWorkloadClusterBinding[*]
      -> Inventory Source
      -> Per-cluster credentials
      -> Per-cluster diagnostic job templates if needed
```

## Testing Plan

### Unit Tests

- Descriptor generation for `LocalWorkloadClusterClass`.
- Inventory naming and variable rendering.
- AWX tenant binding resource-name helpers.
- Inventory script parsing/grouping against fixture CAPI Machine lists.

### Render/Static Checks

- `py_compile` changed modules.
- Unit tests under `pulumi/tests/unit`.
- Validate inventory script executable output is JSON and AWX-compatible.

### Runtime Validation

On local fresh stack:

1. AWX web pod is Ready.
2. AWX provider readiness succeeds against in-cluster service URL.
3. AWX organization/project/inventory/job templates exist.
4. Inventory source sync succeeds.
5. Inventory contains controller and compute groups for `local-workload`.
6. Smoke job template launch succeeds.
7. No manual UI configuration is required.

## Open Questions

- Which Terraform AWX provider should we bridge and pin?
- Does the provider support inventory source sync / job launch resources, or only static object management?
- Do we want Kubernetes-exec-based jobs first, or SSH-to-node jobs first?
- Should AWX tenant bindings live in the workload package or in a control-plane AWX package with tenant descriptors as input?
- What is the minimum RBAC for the AWX Kubernetes credential?

## Recommended First PR Shape

Keep the first implementation small:

1. Bridge AWX provider.
2. Add AWX provider readiness/config component.
3. Add one `AWXTenantBinding` for the local workload cluster.
4. Create one AWX inventory and one project-backed inventory source.
5. Create one read-only cluster-state job template.
6. Leave SSH/machine credentials and Slurm smoke execution for the next pass.
