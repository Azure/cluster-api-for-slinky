# AWX Dynamic Inventory Integration

## Goal

Manage the minimum useful AWX API configuration from the control-plane stack and
let AWX dynamic inventory discover live workload-cluster hosts from CAPI.

The control plane owns AWX as a management-plane service. Workload-cluster code
should not import AWX provider modules or create AWX objects. It only needs to
make workload clusters discoverable through CAPI labels and status.

## Current Approach

`AWXConfiguration` owns:

- shared organization `ca4s`
- shared SCM credential `ca4s-gitops-scm`
- shared GitOps project derived from the Flux `GitRepository`
- read-only management-cluster Kubernetes credential
- injectable CA4S Kubernetes credential type and credential
- one shared dynamic inventory
- one SCM-backed inventory source
- one cluster-state job template

This intentionally avoids per-tenant or per-workload-cluster Pulumi-managed AWX
bindings. AWX's inventory source is the binding. It refreshes live state from the
management cluster and emits groups/hosts dynamically.

## Why No Tenant Binding Component Yet

A first-class `AWXTenantBinding` would duplicate information CAPI already owns
and would require Pulumi to update AWX when autoscaled hosts appear or disappear.
That is the wrong lifecycle for hosts.

Instead:

```text
Pulumi control plane
  -> creates AWX project, credentials, inventory source, job templates

CAPI management cluster
  -> owns live Cluster/Machine state

AWX inventory source
  -> discovers CAPI Machines at refresh time
  -> emits tenant/cluster/role groups and hostvars
```

Tenant concepts can still exist as labels or inventory variables, but they do
not need to be Pulumi component boundaries until we add tenant RBAC or multiple
separate inventories.

## Credential Model

The stock AWX Kubernetes credential type has no injectors in this AWX version, so
it is useful as an AWX object but not sufficient for custom dynamic inventory
scripts that need environment variables.

The control-plane stack therefore creates a CA4S custom credential type with
fields matching the built-in Kubernetes bearer-token credential:

- `host`
- `bearer_token`
- `verify_ssl`
- `ssl_ca_cert`

and injects them into the stock execution environment as:

```text
CA4S_K8S_HOST
CA4S_K8S_BEARER_TOKEN
CA4S_K8S_VERIFY_SSL
CA4S_K8S_SSL_CA_CERT
```

The credential value still comes from a dedicated Kubernetes ServiceAccount token
Secret and read-only RBAC in the AWX namespace. The credential can list CAPI
resources and Nodes, but cannot read Kubernetes Secrets.

## Stock Execution Environment

The default AWX execution environment image `quay.io/ansible/awx-ee:24.6.1`
already contains enough tooling for the first implementation:

- `ansible-runner`
- `ansible-playbook`
- Python `kubernetes`
- Python `yaml`
- Python `requests`

It does not need the old runner enhancements:

- no `/runner/.kube` hostPath/PVC mount
- no `host.docker.internal` kubeconfig rewrite
- no `K8S_AUTH_VERIFY_SSL=false` hack
- no nested `pip install ansible-runner`
- no nested `ansible-playbook` from inside an AWX job

If future playbooks use `kubernetes.core.k8s_info`, we may need a custom
execution environment or collection installation. The dynamic inventory script
itself uses the Python Kubernetes client and works with the stock image.

## Dynamic Inventory Script

Path:

```text
projects/awx/inventory/capi_slurm_inventory.py
```

Responsibilities:

- read CA4S-injected Kubernetes API credential environment variables
- query management-cluster CAPI `Machine` objects
- group hosts by CAPI cluster and Slinky node type
- emit AWX-compatible dynamic inventory JSON

Current discovery defaults:

```text
CAPI group/version: cluster.x-k8s.io/v1beta2
Machine namespace: default
cluster label: cluster.x-k8s.io/cluster-name
node type label: slinky.slurm.net/node-type
controller node type: controller
compute node type: compute
```

The script emits `localhost` as a management host with `ansible_connection=local`
so management API jobs can run before SSH/node execution is wired.

For each CAPI Machine with both a cluster label and node-type label:

- inventory host name comes from `status.addresses[type=Hostname]`, then
  `status.nodeRef.name`, then `metadata.name`
- `ansible_host` comes from `status.addresses[type=ExternalIP]`, falling back to
  `InternalIP`
- hostvars include CAPI cluster, namespace, Machine name, and node type

Expected groups for `local-workload`:

```json
{
  "all": {"children": ["management", "slurm", "cluster_local_workload"]},
  "management": {"hosts": ["localhost"]},
  "slurm": {"children": ["cluster_local_workload", "compute", "controller"]},
  "cluster_local_workload": {
    "children": ["compute_local_workload", "controller_local_workload"]
  },
  "controller": {"children": ["controller_local_workload"]},
  "compute": {"children": ["compute_local_workload"]}
}
```

## AWX Resources

The control-plane stack creates these additional AWX resources:

```text
CredentialType: CA4S Kubernetes API Bearer Token
Credential:     ca4s-management-kubernetes-env
Inventory:      ca4s-dynamic-inventory
Source:         ca4s-capi-slurm
Job template:   ca4s-collect-cluster-state
```

Inventory source shape:

```text
source: scm
source_project: shared GitOps AWX project
source_path: projects/awx/inventory/capi_slurm_inventory.py
credential: ca4s-management-kubernetes-env
overwrite: true
overwrite_vars: true
update_on_launch: true
```

Job template shape:

```text
project: shared GitOps AWX project
inventory: ca4s-dynamic-inventory
playbook: projects/awx/playbooks/collect_cluster_state.yml
credential: ca4s-management-kubernetes-env
```

The provider currently requires `JobTemplateAssociateCredential` for attaching
the credential to the template. The generated provider marks that association
resource as deprecated, but no replacement resource is available in the current
SDK surface.

## Cluster-State Playbook

Path:

```text
projects/awx/playbooks/collect_cluster_state.yml
```

The first playbook runs on `localhost` using the stock execution environment and
executes the dynamic inventory script in `--summary` mode. This validates that
AWX can use the injected Kubernetes credential and inspect management-cluster CAPI
state without SSHing to workload nodes.

## Follow-Ups

1. Add tenant labels to CAPI clusters once tenant config becomes explicit.
2. Add optional cluster-name or label selectors to inventory source variables.
3. Add a Slurm smoke playbook that execs into workload-cluster Slurm pods through
   Kubernetes instead of SSH.
4. Add SSH/node-host execution only after there is a clean managed key story.
5. Replace `JobTemplateAssociateCredential` if the AWX provider exposes a newer
   credential association resource.

## Validation

Unit tests cover:

- stable AWX credential type input/injector JSON
- stable dynamic inventory source variables
- CAPI Machine to AWX dynamic inventory grouping
- dynamic inventory summary rendering

Runtime validation should cover:

1. PKO stack reaches `Ready=True`.
2. AWX inventory source exists and syncs successfully.
3. AWX inventory contains `localhost`, controller, and compute groups.
4. Cluster-state job template launches successfully.
5. Job output shows discovered CAPI clusters/hosts without printing credential
   material.
