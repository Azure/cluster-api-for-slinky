# Autoscaling: From Slurm Jobs to CAPD Nodes

This document explains the end-to-end autoscaling pipeline that dynamically
provisions Kubernetes (CAPD) nodes in response to pending Slurm jobs. The
Pulumi local stack owns both the KEDA NodeSet scaler in the workload cluster
and the Cluster Autoscaler in the management cluster.

## High-Level Flow

```
Slurm busy workers + eligible queued node requests
  → Prometheus scrapes the Slurm capacity collector
    → KEDA scales the NodeSet (slurmd pod count)
      → Unschedulable slurmd pods appear
        → Cluster Autoscaler scales the compute MachineDeployment
          → CAPD creates new Docker "nodes"
            → Nodes join the workload cluster
              → slurmd pods are scheduled and register with Slurm
                → Pending jobs run
```

## Components

### 1. Slurm Capacity → Prometheus

The generated Slurm chart values add a small capacity-collector sidecar to the
controller Pod. It uses the controller image's Python and authenticated Slurm
CLI with the controller's shared authentication socket, without an additional
exporter dependency. A dedicated Service and
ServiceMonitor expose these per-partition gauges:

```
ca4s_slurm_busy_workers{partition="compute"}
ca4s_slurm_pending_workers{partition="compute"}
ca4s_slurm_worker_demand{partition="compute"}
```

The demand gauge is **distinct busy workers plus eligible queued node
requests**, not merely the number of pending jobs:

| Busy workers | Eligible pending node requests | Desired workers before min/max bounds |
|---|---|---|
| 1 | 1 | 2 |
| 3 | 2 | 5 |
| 3 | 0 | 3 |

Allocated, mixed and completing nodes are counted once, even if several jobs
share a worker. For pending jobs, the collector sums minimum node requests,
including multi-node jobs, rather than counting each job as one worker. Array
tasks respect the array's concurrency limit and already active tasks. A job
eligible for several configured partitions contributes to only the first
permitted partition in its request.

Eligible pending reasons are `Resources`, `Priority` and `PartitionNodeLimit`.
The last is included only when the request fits both the partition's configured
limit and the NodeSet's maximum replica count: a valid two-node request can have
this reason while only one Slurm node is registered. Held, dependent,
license-blocked, not-yet-evaluated, and other policy-blocked jobs do not add
demand. Neither do requests tied to specific existing nodes or requests beyond
the configured capacity bounds. Excluded counts are exposed as
`ca4s_slurm_excluded_pending_jobs{partition,reason}`.

This is an exclusive-job capacity model for compatible, homogeneous workers
in disjoint partitions, not a replacement for Slurm scheduling. Shared jobs
are conservatively treated as whole-node requests; CPU/GPU bin packing and
arbitrary feature-constraint feasibility are not estimated. The operator's
workload-aware drain protections remain the final authority on worker removal.

The collector takes fresh CLI snapshots on each scrape. Failed commands,
invalid JSON or missing partitions produce HTTP 503 rather than zero demand.
The scaler excludes unhealthy scrape targets and rejects an empty result,
preventing unavailable telemetry from being interpreted as spare capacity.
`max` deduplicates scrape copies instead of summing the same demand twice.
The built-in slurmctld metrics remain enabled for monitoring.

### 2. KEDA ScaledObject → NodeSet Replicas

Pulumi installs KEDA in the workload cluster and creates a `ScaledObject` that
watches the capacity-demand metric and maps it to the replica count of a Slinky
**NodeSet**:

```yaml
spec:
  scaleTargetRef:
    apiVersion: slinky.slurm.net/v1beta1
    kind: NodeSet
    name: <slurm-release>-worker-compute
  minReplicaCount: 1
  maxReplicaCount: 10
  triggers:
  - type: prometheus
    metadata:
      query: >-
        max(ca4s_slurm_worker_demand{namespace="slurm",partition="compute"}
        and on (job, instance)
        (up{namespace="slurm",service="slurm-capacity-metrics"} == 1))
      threshold: '1'
      ignoreNullValues: 'false'
```

The activation threshold is omitted, using KEDA's default of zero. It applies
only when scaling from zero; with `minReplicaCount: 1`, activation does not gate
scaling. The positive scaling threshold is one worker-equivalent per desired
replica, subject to the configured minimum and maximum. Each replica becomes
one `slurmd` pod, essentially one Slurm compute node. When a queued one-node job
starts, its pending demand becomes a busy worker, leaving the total unchanged.
Only when jobs finish does that occupied capacity leave the target. KEDA/HPA
stabilization still applies before reducing replicas.

### 3. Unschedulable Pods → Cluster Autoscaler → MachineDeployment

Because NodeSet oversubscription is disabled (see below), each slurmd pod requires
its own dedicated Kubernetes node. When KEDA requests more NodeSet
replicas than there are available compute nodes, the new pods become
*unschedulable*.

The **Cluster Autoscaler** runs on the management cluster and watches the
workload cluster through the CAPI-generated workload kubeconfig. The local
Pulumi workload-cluster class installs it as a first-class component and it
responds to unschedulable pods by scaling the compute CAPI
**MachineDeployment**:

```yaml
cloudProvider: clusterapi
clusterAPIMode: kubeconfig-incluster
clusterAPIKubeconfigSecret: local-autoscaler-kubeconfig
clusterAPIWorkloadKubeconfigPath: /etc/kubernetes/value
autoDiscovery:
  namespace: default
  clusterName: local-workload
extraArgs:
  scale-down-unneeded-time: 2m
```

Key details:

- **Auto-discovery** is scoped to the CAPI cluster name. This keeps the
  MachineDeployment, MachineSet, and Machine lookup path under the same filter,
  which Cluster Autoscaler needs when mapping workload Nodes back to CAPI
  Machines.
- **Min/max bounds** are controlled by annotations on the MachineDeployment
  itself (see label/annotation passthrough below).
- **Replica ownership** belongs to Cluster Autoscaler. Pulumi creates the
  MachineDeployment without `spec.replicas` for autoscaled worker classes and
  ignores drift on `spec.replicas` so PKO does not overwrite autoscaler changes.
- **Scale-down** is set to 2 minutes of idle time for demo purposes.
- **CAPD RBAC** is owned by Pulumi and grants access to the
  `infrastructure.cluster.x-k8s.io` API group, which the default Helm chart
  does not include.

### 4. CAPD Creates Docker Nodes

The compute MachineDeployment's infrastructure reference points to a
`DockerMachineTemplate`. When the Cluster Autoscaler increases the
MachineDeployment replica count, CAPI/CAPD creates new Docker containers that
act as Kubernetes nodes. These nodes are bootstrapped with kubeadm and join the
workload cluster.

---

## Label and Annotation Passthrough

Labels and annotations on CAPI resources must propagate all the way down to
the actual Kubernetes `Node` objects so that pod scheduling rules work
correctly. This is the critical glue that ties CAPI provisioning to Slurm
pod placement.

### Where Labels Originate

The local workload cluster has one `KubeadmControlPlane`, one fixed controller
`MachineDeployment`, and one autoscaled compute `MachineDeployment`:

```yaml
workers:
- name: local-head
  replicas: 1
  labels:
    slinky.slurm.net/node-type: controller

- name: local-compute
  # replicas omitted: Cluster Autoscaler owns the live value
  metadata:
    annotations:
      cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size: '1'
      cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size: '10'
    labels:
      slinky.slurm.net/node-type: compute
      ca4s.azure.com/autoscaler-enabled: "true"
```

- **`slinky.slurm.net/node-type: controller`** on the fixed controller
  MachineDeployment identifies the worker that runs Slurm head and platform
  services.
- **`slinky.slurm.net/node-type: compute`** on the compute MachineDeployment
  identifies nodes that run slurmd worker pods.
- **`ca4s.azure.com/autoscaler-enabled: "true"`** is a metadata marker on the
  MachineDeployment. Cluster Autoscaler discovery is scoped by cluster name so
  the related MachineSets and Machines remain discoverable during Node mapping.
- The **autoscaler annotations** tell Cluster Autoscaler the allowed scaling
  range (1–10 nodes).

### How Labels Reach Kubernetes Nodes

Controller and compute labels follow the CAPI propagation path from
`MachineDeployment` to `Machine` to `Node`. CAPI only propagates labels that
match a configured allowlist, so the local management setup enables Slinky
labels:

```bash
kubectl -n capi-system patch deployment/capi-controller-manager \
  --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-",
        "value":"--additional-sync-machine-labels=.*slinky\\.slurm\\.net.*"}]'
```

This tells the CAPI controller to sync any label matching
`.*slinky\.slurm\.net.*` from Machine to Node. Without it, worker nodes would
not carry their controller/compute roles and required pod affinity would fail.

### Propagation Chain

```
Pulumi controller/compute worker classes
  → MachineDeployment metadata.labels
    → Machine metadata.labels
      → Node role labels  (via --additional-sync-machine-labels)

controller Node label → platform pod affinity
compute Node label    → slurmd pod affinity
```

---

## NodeSet Oversubscription: One slurmd Per Node

The generated Slurm Helm values disable NodeSet oversubscription:

```yaml
nodesets:
  compute:
    oversubscribeNode: false
```

The Slurm Operator then adds hostname-level pod anti-affinity, keeping one
slurmd pod on each compute node. Excess replicas remain pending, prompting
Cluster Autoscaler to grow the compute MachineDeployment.

---

## Controller Node Placement

Head-node services — `slurmctld`, `loginsets`, and `restapi` — each carry
the same node affinity:

```yaml
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
      - matchExpressions:
        - key: slinky.slurm.net/node-type
          operator: In
          values:
          - controller
```

This same role selects the dedicated controller MachineDeployment on local CAPD
and Azure BYO, and the fixed System pool on AKS. Kubernetes control-plane nodes
are not application placement targets.

Other non-DaemonSet platform components are also pinned to the controller role,
including `cert-manager`, `slurm-operator`, `kube-prometheus-stack`,
`local-path-provisioner`, `coredns`, and `calico-kube-controllers`. The goal is
that no ordinary platform Deployment lands on a compute node and prevents
Cluster Autoscaler from removing that node when Slurm demand drops. DaemonSets
such as `calico-node`, `kube-proxy`, and `prometheus-node-exporter` still run on
every node; Cluster Autoscaler understands DaemonSet overhead and they do not
block scale-in by themselves.

---

## Controller Taint Isolation

Every project-managed controller worker/System pool has one taint:

```yaml
CriticalAddonsOnly=true:NoSchedule
```

CAPS treats its pinned platform controllers as critical add-ons across CAPD,
Azure BYO, and AKS. Platform pods explicitly tolerate this conventional taint
and use required controller-role affinity. Compute workers are untainted, and
Slurm `NodeSet` pods select them with compute-role affinity. The taint keeps
unconstrained tenant pods off controller workers, while the project-specific
`slinky.slurm.net/node-type` label keeps platform Deployments off compute
workers.

Self-managed Kubernetes control-plane nodes retain kubeadm's standard
`node-role.kubernetes.io/control-plane:NoSchedule` taint. That provider-owned
taint is not managed or tolerated by the platform workloads because those pods
target the dedicated controller worker instead.

Placement is rendered directly by the Pulumi workload-cluster class. There are
no separate root-level Helm values files for the managed local stack.

### Who Tolerates the Taint

| Component | Tolerates `CriticalAddonsOnly`? | How |
|-----------|-----------------------------------|-----|
| Slurm controller/login/restapi | Yes | Generated chart values add the toleration and controller-role affinity |
| Pinned platform Deployments | Yes | Generated Pulumi/Helm values add the toleration plus controller-role affinity |
| slurmd NodeSet workers | No | They select compute nodes by `slinky.slurm.net/node-type: compute` |
| DaemonSets | Usually yes | Kubernetes DaemonSet defaults or chart tolerations allow node agents everywhere |
| Tenant workloads | No | They stay off controller nodes unless explicitly configured |

AKS uses the same contract on its fixed System pool. Its Azure-managed control
plane is not exposed as schedulable Nodes, so the System pool is the controller
role target.

### NodeSet Placement

```yaml
nodesets:
  slinky:
    podSpec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: slinky.slurm.net/node-type
                operator: In
                values:
                - compute
```

---

## Summary Diagram

![Autoscaling flow from Slurm jobs to CAPD nodes](images/autoscaling.svg)

[Open the autoscaling SVG](images/autoscaling.svg)

[View the Graphviz source](images/autoscaling.dot)
