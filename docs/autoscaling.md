# Autoscaling: From Slurm Jobs to CAPD Nodes

This document explains the end-to-end autoscaling pipeline that dynamically
provisions Kubernetes (CAPD) nodes in response to pending Slurm jobs.

## High-Level Flow

```
Slurm pending jobs
  → Prometheus scrapes slurmctld metrics
    → KEDA scales the NodeSet (slurmd pod count)
      → Unschedulable slurmd pods appear
        → Cluster Autoscaler scales the MachinePool
          → CAPD creates new Docker "nodes"
            → Nodes join the workload cluster
              → slurmd pods are scheduled and register with Slurm
                → Pending jobs run
```

## Components

### 1. Slurm Metrics → Prometheus

The `slurmctld` controller exposes a built-in Prometheus metrics endpoint
(enabled via `controller.metrics.enabled: true` in `slurm-cluster.yaml`).
A `ServiceMonitor` lets Prometheus scrape this endpoint automatically. The
key metric is:

```
slurm_partition_jobs_pending{partition="all"}
```

This is the number of Slurm jobs waiting for compute resources.

### 2. KEDA ScaledObject → NodeSet Replicas

A KEDA `ScaledObject` (`nodeset-scaledobject.yaml`) watches the pending-jobs
metric and maps it to the replica count of a Slinky **NodeSet**:

```yaml
spec:
  scaleTargetRef:
    apiVersion: slinky.slurm.net/v1beta1
    kind: NodeSet
    name: slurm-worker-slinky
  minReplicaCount: 1
  maxReplicaCount: 10
  triggers:
  - type: prometheus
    metadata:
      query: slurm_partition_jobs_pending{partition="all"}
      threshold: '1'
      activationThreshold: '1'
```

When at least one job is pending, KEDA increases the NodeSet replica count
(up to 10). Each replica becomes one `slurmd` pod—essentially one Slurm
compute node. When pending jobs drop to zero, KEDA scales back down (minimum
of 1 replica).

### 3. Unschedulable Pods → Cluster Autoscaler → MachinePool

Because of the **anti-affinity** rule (see below), each slurmd pod requires
its own dedicated Kubernetes node. When KEDA requests more NodeSet replicas
than there are available compute nodes, the new pods become *unschedulable*.

The **Cluster Autoscaler** (running on the management cluster, configured in
`cluster-autoscaler.yaml`) detects these unschedulable pods and responds by
scaling up the CAPI **MachinePool**:

```yaml
cloudProvider: clusterapi
clusterAPIMode: kubeconfig-incluster
clusterAPIWorkloadKubeconfigPath: /mnt/kubeconfig/capi-quickstart.kubeconfig
autoDiscovery:
  namespace: default
  labels:
  - slinky.slurm.net/node-type: compute
extraArgs:
  scale-down-unneeded-time: 2m
```

Key details:

- **Auto-discovery** finds the MachinePool by the label
  `slinky.slurm.net/node-type: compute`.
- **Min/max bounds** are controlled by annotations on the MachinePool itself
  (see label/annotation passthrough below).
- **Scale-down** is set to 2 minutes of idle time for demo purposes.
- **CAPD RBAC** (`cluster-autoscaler-capd-rbac.yaml`) grants the autoscaler
  access to the `infrastructure.cluster.x-k8s.io` API group, which the
  default Helm chart does not include.

### 4. CAPD Creates Docker Nodes

The MachinePool's infrastructure reference points to a
`DockerMachinePoolTemplate`. When the Cluster Autoscaler increases the
MachinePool replica count, CAPD creates new Docker containers that act as
Kubernetes nodes. These nodes are bootstrapped with kubeadm and join the
workload cluster.

---

## Label and Annotation Passthrough

Labels and annotations on CAPI resources must propagate all the way down to
the actual Kubernetes `Node` objects so that pod scheduling rules work
correctly. This is the critical glue that ties CAPI provisioning to Slurm
pod placement.

### Where Labels Originate

In `capi-quickstart.yaml`, the Cluster topology declares:

```yaml
workers:
  machineDeployments:
  - class: default-worker
    name: md-0
    replicas: 1
    metadata:
      labels:
        slinky.slurm.net/node-type: controller   # ← head-node label

  machinePools:
  - class: default-worker
    name: mp-0
    metadata:
      annotations:
        cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size: '1'
        cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size: '10'
      labels:
        slinky.slurm.net/node-type: compute       # ← compute-node label
```

- **`slinky.slurm.net/node-type: controller`** on the MachineDeployment
  identifies nodes that run the Slurm head services (slurmctld, login,
  REST API).
- **`slinky.slurm.net/node-type: compute`** on the MachinePool identifies
  nodes that run slurmd worker pods.
- The **autoscaler annotations** tell Cluster Autoscaler the allowed scaling
  range (1–10 nodes).

### How Labels Reach Kubernetes Nodes

CAPI's controller manager propagates labels from `Machine` objects to
`Node` objects, but only for labels that match a configured allowlist. Custom
Slinky labels require an explicit patch:

```bash
kubectl -n capi-system patch deployment/capi-controller-manager \
  --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-",
        "value":"--additional-sync-machine-labels=.*slinky\\.slurm\\.net.*"}]'
```

This tells the CAPI controller to sync any label matching
`.*slinky\.slurm\.net.*` from Machine to Node. Without this patch,
MachineDeployment-created nodes would not carry the `slinky.slurm.net/*`
labels and pod affinity rules would fail.

> **Note:** CAPD MachinePool labels propagate to nodes without this patch;
> the patch is specifically needed for MachineSet/Machine-based topologies
> (i.e., the MachineDeployment used for the controller node).

### Propagation Chain

```
Cluster topology (capi-quickstart.yaml)
  → MachineDeployment / MachinePool metadata.labels
    → Machine metadata.labels
      → Kubernetes Node labels  (via CAPI controller --additional-sync-machine-labels)
        → Pod nodeAffinity selectors match  (slurm-cluster.yaml)
```

---

## Anti-Affinity: One slurmd Per Node

The NodeSet in `slurm-cluster.yaml` declares a **hard pod anti-affinity**
rule on the compute pods:

```yaml
nodesets:
  slinky:
    podSpec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: "slinky.slurm.net/node-type"
                operator: In
                values:
                - "compute"
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector:
              matchExpressions:
              - key: app.kubernetes.io/name
                operator: In
                values:
                - "slurmd"
            topologyKey: "kubernetes.io/hostname"
```

This enforces two constraints:

1. **Node affinity** — slurmd pods can only land on nodes labeled
   `slinky.slurm.net/node-type: compute`. This keeps them off the
   controller and control-plane nodes.
2. **Pod anti-affinity** — no two pods with label
   `app.kubernetes.io/name: slurmd` may share the same hostname (node).
   This guarantees a strict **1:1 mapping between slurmd pods and compute
   nodes**.

### Why Anti-Affinity is Critical for Autoscaling

The anti-affinity rule is what *bridges* KEDA scaling to infrastructure
scaling:

1. KEDA increases the NodeSet to N replicas (N slurmd pods requested).
2. If only M < N compute nodes exist, the remaining N − M pods are
   **unschedulable** — the scheduler cannot place two slurmd pods on the
   same node.
3. Cluster Autoscaler sees the unschedulable pods and grows the MachinePool
   until N compute nodes exist.
4. New nodes get the `compute` label via CAPI label passthrough, and the
   pending slurmd pods are immediately scheduled.

Without anti-affinity, Kubernetes could pack multiple slurmd pods onto the
same node, and the Cluster Autoscaler would never be triggered — defeating
the purpose of dedicated compute node provisioning.

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
        - key: "slinky.slurm.net/node-type"
          operator: In
          values:
          - "controller"
```

This ensures all Slurm management components run on the MachineDeployment
node (`md-0`) rather than on compute MachinePool nodes, keeping head-node
traffic isolated from worker workloads.

---

## Compute Node Taint Isolation

Node affinity alone does not prevent non-Slurm pods (e.g., Grafana,
cert-manager, CoreDNS) from landing on compute nodes. To enforce strict
"Slurm-only" compute nodes, a **taint** is applied at node registration
time.

### Taint on Compute Nodes

The MachinePool uses a dedicated `KubeadmConfigTemplate`
(`quick-start-compute-worker-bootstraptemplate`) that adds a taint during
kubeadm join:

```yaml
joinConfiguration:
  nodeRegistration:
    taints:
    - key: slinky.slurm.net/compute
      effect: NoSchedule
```

This prevents any pod from scheduling onto compute nodes unless it
explicitly tolerates the taint.

### Who Tolerates the Taint

| Component | Tolerates? | How |
|-----------|-----------|-----|
| slurmd (NodeSet worker) | Yes | `slurm-cluster.yaml` → `nodesets.slinky.podSpec.tolerations` |
| kube-proxy | Yes | DaemonSet has `operator: Exists` (tolerates all) |
| calico-node (CNI) | Yes | DaemonSet has `operator: Exists` (tolerates all NoSchedule) |
| prometheus-node-exporter | Yes | DaemonSet has `operator: Exists` (tolerates all NoSchedule) |
| Everything else | No | Pods like CoreDNS, Grafana, cert-manager stay on controller/CP nodes |

### NodeSet Toleration in `slurm-cluster.yaml`

```yaml
nodesets:
  slinky:
    podSpec:
      tolerations:
      - key: slinky.slurm.net/compute
        operator: Exists
        effect: NoSchedule
```

---

## Summary Diagram

```
┌──────────────────────── Management Cluster (Kind) ───────────────────────┐
│                                                                          │
│  Cluster Autoscaler ──discovers──▶ MachinePool (mp-0)                    │
│         │                          label: compute                        │
│         │                          annotations: min=1, max=10            │
│         │                                  │                             │
│         │ scales up/down                   │ CAPD creates Docker nodes   │
│         ▼                                  ▼                             │
│  ┌─────────────────────── Workload Cluster ────────────────────────┐     │
│  │                                                                 │     │
│  │  Prometheus ◀── scrapes ── slurmctld (metrics)                  │     │
│  │      │                        ▲                                 │     │
│  │      ▼                        │ on controller node              │     │
│  │    KEDA ──scales──▶ NodeSet (slurm-worker-slinky)               │     │
│  │                        │                                        │     │
│  │                        ├─▶ slurmd pod ──▶ compute node 1        │     │
│  │                        ├─▶ slurmd pod ──▶ compute node 2        │     │
│  │                        └─▶ slurmd pod ──▶ compute node N        │     │
│  │                           (1:1, enforced by anti-affinity)      │     │
│  └─────────────────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────────┘
```
