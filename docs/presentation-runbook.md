# Team Demo Runbook — CAPS self-managed HPC cluster

Live demo driver: `scripts/demo-presentation.sh {clusters|nodes|slurm|job|bw|nccl|all}`

## One-line story
"I stood up a **self-managed Kubernetes cluster on Azure with Cluster API (CAPZ)**,
running an **HPC image** with **Slurm + HPC-X MPI**, and I can launch real
**cross-node MPI jobs over RDMA/InfiniBand** through the scheduler — the
foundation for PBS→Slurm HPC migration."

## Pre-demo checklist (run 10 min before)
```bash
cd ~/CAPS/caps-pulumi
bash scripts/azure-remote.sh sync          # push latest
./scripts/demo-presentation.sh clusters    # confirm all machines Running
./scripts/demo-presentation.sh slurm       # confirm 2 compute nodes idle
```

> The demo script is **self-narrating**: each section prints `context` →
> `watch:` → `takeaway:` around the raw output, so you barely have to talk.
> For a live, step-through run that pauses between sections:
> `PAUSE=1 ./scripts/demo-presentation.sh all`

## Demo flow (~6 min)
1. **clusters** — "Cluster API provisioned this: 1 control plane + 3 worker
   MachineDeployment VMs (2 GPU compute + 1 Slurm-controller), all `Running`.
   Fully self-managed on Azure."
2. **nodes** — "The 2 compute nodes are **Standard_ND40rs_v2** (8× V100 GPU +
   100Gb EDR InfiniBand) on the `ubuntu-hpc` image — HPC-X, OFED, CUDA baked in.
   A separate small node hosts the Slurm controller. All `Ready`."
3. **slurm** — "Slurm (slinky operator) on top: slurmctld on its own node + 2
   compute nodes `idle`, ready for jobs."
4. **job** — "Live: OSU MPI latency across both nodes, run **twice** — TCP then
   RDMA — via Slurm/PMIx + HPC-X. **~22 µs over TCP vs ~1.8 µs over RDMA**,
   ~12× faster, same hardware, only the UCX transport changed." (then **bw** →
   TCP ~3 GB/s vs **RDMA ~11 GB/s**)
   > The comparison is the money shot: identical `srun` job, flip `UCX_TLS=tcp`
   > → `rc` and the InfiniBand fabric lights up. No hardware change.
5. **nccl** — "Finale: multi-GPU **NCCL all-reduce** across **16 V100s** (8/node)
   over InfiniBand — **host-launch** (Slurm allocates, mpirun runs on the worker
   host over SSH). Peak **~15.7 GB/s** bus bandwidth, correctness `#wrong=0`."
   > The real GPU collective HPC/AI training scales on — not just point-to-point MPI.

## What to highlight as progress
- Self-managed CAPZ cluster on a BYO Azure VNet (worked around corp NRMS + NSG constraints).
- Full Day-2: cloud-provider-azure, Calico (VXLAN — IPIP is dropped by Azure fabric).
- HPC-X MPI launched through Slurm — the team's PBS NCCL flow mapped to Slurm prototype.

## Next steps slide
- RDMA over InfiniBand + multi-GPU NCCL all-reduce both working now.
- Next: scale to more nodes; port real validation-pipeline workloads onto this.
- Fleet-repair health checks + node replacement; custom slurmd image for production.

## Demo numbers (captured 2026-07-08)
- MPI latency: TCP ~22 µs vs **RDMA ~1.8 µs** (small msg) — ~12× faster.
- MPI bandwidth: TCP ~3.1 GB/s vs **RDMA ~11 GB/s** peak — ~3.5× (near 100Gb EDR line-rate).
- NCCL all-reduce (16× V100 over IB): peak **~15.7 GB/s** bus bandwidth, `#wrong=0`.

## Gotcha if MPI errors live
- TCP path: "No route to host" → UCX grabbed a stale Calico iface; the script pins
  `UCX_NET_DEVICES=eth0`. RDMA path pins `UCX_NET_DEVICES=mlx5_ib0:1`; if the IB
  device name differs, override `UCX_RDMA="UCX_TLS=rc UCX_NET_DEVICES=<dev>:1"`.
- If a node shows `down`: `scontrol update node=<n> state=resume`.

---

## Anticipated Q&A — HPC & AI Image team SWEs
Frame for context: their `hpc-image-val` builds/validates images (Packer, single
VM, SBOM/Trivy → Kusto) and `hpc-image-val2` benchmarks them (headnode + VMSS,
**OpenPBS**, NCCL/OSU/HPC-X, NHC faultcodes, telemetry → `ImagePerf`/PowerBI).
CAPS is the same workloads on **CAPI/CAPZ + Kubernetes + Slurm (slinky)**.

### How it maps to your existing pipeline
- **"What replaces PBS?"** Slurm via the slinky operator. `sbatch`/`srun --mpi=pmix`
  replaces `qsub`/`mpirun-over-SSH`. Same OSU/NCCL binaries, same HPC-X — only the
  launcher changes. I already mapped `queue-nccl-pbs.sh`/`run-nccl.pbs` →
  `scripts/nccl-slurm/` (drop-in, same positional args, `%x.o%j` keeps ingestion identical).
- **"What replaces the headnode + VMSS?"** Control-plane = CAPI mgmt; compute = a
  MachineDeployment of VMs (same ubuntu-hpc image you publish). Scaling = bump
  `replicas`, non-destructive. No bespoke `build_cluster.sh` retry/backoff —
  CAPZ reconciles capacity.
- **"Same image?"** Yes — `microsoft-dsvm:ubuntu-hpc` (HPC-X, OFED, CUDA, NCCL).
  Directly consumes what `hpc-image-val` produces; no separate image needed.
- **"PBS failure-detection (PR 740)?"** Mirrored: `check_slurm_exit_codes` via
  `sacct` catches NODE_FAIL/TIMEOUT/OOM, not just exit code — stronger than PBS.

### Benchmarks / MPI
- **"Does RDMA/InfiniBand actually work?"** Yes — live in the demo. Same `srun`
  job over TCP (~22 µs) vs RDMA `UCX_TLS=rc` on `mlx5_ib0` (~1.8 µs); bandwidth
  ~3 GB/s → ~11 GB/s. `/dev/infiniband` mounted, UCX shows `rc_verbs` transport.
- **"NCCL on this?"** Yes — live: 16× V100 all-reduce over IB via **host-launch**
  (`scripts/nccl-slurm/submit-nccl-host.sh`), ~15.7 GB/s bus bw, `#wrong=0`. Slurm
  allocates the nodes; mpirun runs on the worker host over SSH (native /dev/infiniband).
- **"HPC-X getting in?"** Bind-mounted host `/opt/hpcx` for the dev path;
  production = custom slurmd image FROM ubuntu-hpc pushed to ACR (your team's pattern).
- **"InfiniBand setup?"** Privileged + hostNetwork slurmd pods + `/dev/infiniband`
  (matches your nccl-test.yaml). No RDMA device plugin needed on this SKU.

### Telemetry / validation (your Kusto + NHC story)
- **"Goes to ImagePerf/ADX?"** Wrappers keep PBS output filenames identical so
  `generate_telemetry_*.sh` + `tokustocluster.py` work unchanged → same tables/PowerBI.
- **"NHC faultcodes?"** Not wired yet; node health = K8s NotReady + Slurm drain.
  NHC + `nhc_text_faultcode.json` is a clean follow-up.

### Why bother vs PBS+VMSS
- Self-healing, declarative (GitOps) scaling, one orchestrator for sched + service
  workloads. Trade-off: more moving parts (CNI, cloud-provider, BYO-VNet/NSG vs corp NRMS).
- **Not** replacing image-build/Packer/SBOM — sits where `hpc-image-val2` does (perf/bench).

### Likely "gotcha" questions
- **"GPU/IB demoed?"** Yes — live: RDMA MPI (~1.8 µs / ~11 GB/s) AND multi-GPU
  NCCL all-reduce over IB (~15.7 GB/s, 16× V100) on 2× ND40rs_v2.
- **"Pulumi reconciled?"** Self-managed manifest is manual; mentor folds into Pulumi later.
- **"Multi-tenant scale?"** 2-compute-node proof; scale via MachineDeployment `replicas`.
