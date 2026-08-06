# CAPS infrastructure handoff

This document covers the infrastructure half of the CAPS image-validation path.
The benchmark and publication half is in `hpc-image-val/slurm_validation` on
branch `dev/t-hernandezc/caps-infra-pipeline`.

## Code walkthrough

- `pulumi/stacks/workload_cluster/workload_cluster_class_azure_byo.py` defines
  the user-facing Azure BYO workload configuration and composes infrastructure
  with Slinky deployments.
- `workload_cluster_infrastructure_azure_byo.py` creates the CAPI/CAPZ objects,
  empty VMSS Flex container, node templates, Kubernetes bootstrap, networking,
  cloud provider, Calico, local storage, and autoscaling resources.
- `workload_cluster_deployments.py` installs Slinky Slurm, KEDA, monitoring, and
  accounting components into the workload cluster.
- `scripts/vm-bootstrap.sh` prepares the per-run management VM, creates the kind
  management cluster, and starts the Pulumi/PKO reconciliation path.
- `scripts/slurm-host-mpi.sh` turns an exclusive Slurm allocation into a
  host-namespace MPI/NCCL launch.
- `scripts/nccl-slurm/run-nccl-host.sh`, `scripts/ib-osu-loopback.sh`, and
  `scripts/superbench/run-superbench-host.sh` are the worker-host launchers.

## Temporary choices and design compromises

### Availability zone 2

The Flex VMSS, machine failure domain, and internal API load balancer are pinned
to zone 2. This matched the validated West Europe ND96 A100 allocation and the
D-series control-plane availability. It is not a general region/SKU policy.

Before adding another region or SKU, expose the zone in workload configuration
and validate availability for every selected SKU. A later design should discover
the intersection of supported zones and fail before provisioning if none exists.

### Flex VMSS and CAPZ fork

RDMA workers use VMSS Flexible with platform fault-domain count 1 to preserve
InfiniBand placement. The CAPZ source is the `arsdragonfly/md-vmss` fork because
the required externally managed VMSS attachment was not available in the pinned
upstream path when this experiment began.

Track the upstream feature and migrate to a released CAPZ contract. Treat that
migration as a compatibility change: validate CAPI/CAPZ versions, CRDs, worker
placement, matching InfiniBand partitions, and teardown before removing the fork.

### Kubernetes version and bootstrap

Kubernetes `v1.36.1` is pinned to the tested CAPI/CAPZ and package repositories.
Explicit HPC images do not contain Kubernetes, so cloud-init installs containerd,
kubelet, kubeadm, and kubectl before CAPI bootstrap. Ubuntu/apt is the only path
validated end to end; RPM-family branches follow upstream package instructions
but remain unproven.

Create a compatibility matrix for image OS, Kubernetes, CAPI, CAPZ, containerd,
and NVIDIA runtime versions. Promote an OS only after a clean unattended run.

### Autoscaling ceiling

The default KEDA maximum of ten workers was chosen for experimentation, not from
subscription quota or cost policy. The NodeSet floor initially equals provisioned
workers so Slurm immediately sees the complete cluster.

Move both bounds into workload configuration and validate them against Azure
quota, SKU availability, benchmark intent, and an explicit cost limit.

### Local-path storage

The cluster installs local-path provisioner as the default StorageClass. It is a
stopgap that made Slurm/accounting bring-up self-contained, but volumes are tied
to one node and can become unusable after controller replacement.

Use a supported durable Azure CSI backend before treating Slurm state or MariaDB
accounting as durable. Test controller replacement and restore behavior.

### Host launch and temporary SSH material

Slurm allocates nodes, but MPI/NCCL execute on worker hosts over SSH for native
access to the image's GPU and RDMA stack. The management VM creates a workload
key, and each allocation temporarily places an allocation-specific copy on its
head worker for OpenMPI peer launch. Cleanup removes it on normal and trapped
exit paths.

This avoids a custom GPU-enabled slurmd image but is not the desired credential
model. Evaluate host-installed slurmd/PMIx, certificates, or short-lived
credentials before productionizing host launch.

### Shared network and resource-group discovery

The management VM is placed in a pre-existing regional 1ES subnet. Pulumi uses
IMDS/ARM to reuse that subnet and the run resource group. This fits the current
one-management-VM-per-run pipeline and private SCP path, but couples provisioning
to the host VM's placement and permissions.

Add explicit network/resource-group inputs if callers must target infrastructure
other than the management VM's discovered environment. Preserve the rule that
the pipeline never owns or deletes the shared 1ES VNet.

### Security tagging

AzSecPack onboarding is automatically added when the API server is public. The
exact organization-wide requirement for internal VMs has not been settled in
this code; older manifests tag all VMs.

Confirm policy with the security owner and make tagging unconditional or
resource-specific based on that decision.

### Retry behavior

kind/ctlptl bootstrap retries once after deleting only the failed cluster, and
cert-manager uses a stable release name to survive partial-install retry. These
handle observed transient failures, but they are fixed retry policies rather
than a broader reconciliation SLO.

Record attempt timing and failure categories, then tune bounded backoff from
observed data. Do not add unbounded retries that hide deterministic failures.

## Safe change checklist

1. Keep zone, VMSS fault-domain, and InfiniBand partition behavior together.
2. Test Kubernetes/CAPI/CAPZ/image upgrades as one compatibility unit.
3. Run focused Pulumi unit tests before any live preview.
4. Use a fresh unattended pipeline run for provisioning changes; do not repair
   the proof cluster in place.
5. Confirm temporary SSH files, Slurm allocations, reporting identities, and
   retained resource groups are cleaned after the run.