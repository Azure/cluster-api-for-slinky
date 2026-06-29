# Team Demo Runbook — CAPS self-managed HPC cluster

Live demo driver: `scripts/demo-presentation.sh {clusters|nodes|slurm|job|bw|all}`

## One-line story
"I stood up a **self-managed Kubernetes cluster on Azure with Cluster API (CAPZ)**,
running an **HPC image** with **Slurm + HPC-X MPI**, and I can launch real
**cross-node MPI jobs** through the scheduler — the foundation for PBS→Slurm
HPC migration."

## Pre-demo checklist (run 10 min before)
```bash
cd ~/CAPS/caps-pulumi
bash scripts/azure-remote.sh sync          # push latest
./scripts/demo-presentation.sh clusters    # confirm 3 machines Running
./scripts/demo-presentation.sh slurm       # confirm 2 nodes idle
```

> The demo script is **self-narrating**: each section prints `context` →
> `watch:` → `takeaway:` around the raw output, so you barely have to talk.
> For a live, step-through run that pauses between sections:
> `PAUSE=1 ./scripts/demo-presentation.sh all`

## Demo flow (~6 min)
1. **clusters** — "Cluster API provisioned this: 1 control plane + 2-node worker
   MachinePool, all `Running`, ~2 days uptime. Fully self-managed on Azure."
2. **nodes** — "Workers are **Standard_ND40rs_v2** (8× V100 GPU + 100Gb EDR
   InfiniBand) on the `ubuntu-hpc` image — HPC-X, OFED, CUDA baked in. 1
   control-plane + 2 HPC workers, all `Ready`."
3. **slurm** — "Slurm (slinky operator) on top: controller + 2 compute nodes
   `idle`, ready for jobs."
4. **job** — "Live: OSU MPI latency across both nodes via Slurm/PMIx + HPC-X.
   ~23 µs cross-node **over TCP**." (then **bw** → ~3.1 GB/s)
   > Demoing the **TCP** transport (`UCX_TLS=tcp`) on purpose: it's the proven,
   > reliable path. The hardware has real InfiniBand — enabling RDMA
   > (`UCX_TLS=rc`) is the very next step, not a hardware change.

## What to highlight as progress
- Self-managed CAPZ cluster on a BYO Azure VNet (worked around corp NRMS + NSG constraints).
- Full Day-2: cloud-provider-azure, Calico (VXLAN — IPIP is dropped by Azure fabric).
- HPC-X MPI launched through Slurm — the team's PBS NCCL flow mapped to Slurm prototype.

## Next steps slide
- Already on GPU + InfiniBand hardware (ND40rs_v2, V100 + EDR IB). Immediate next
  step: flip the transport `UCX_TLS=tcp`→`rc` for real RDMA (no hardware change).
- NCCL all-reduce across the 8×V100 nodes once RDMA is enabled.
- Custom slurmd image w/ HPC-X for IB/production; mentor folds manifest into Pulumi.

## Demo numbers (captured 2026-06-29)
- Cross-node MPI latency: ~23 µs (small msg) | Bandwidth: ~3.1 GB/s peak (TCP).

## Gotcha if MPI errors live
"No route to host" → UCX grabbed a stale Calico iface. The script already pins
`UCX_NET_DEVICES=eth0`. If a node shows `down`: `scontrol update node=<n> state=resume`.

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
  MachinePool over a VMSS (same ubuntu-hpc image you publish). Scaling = bump
  `replicas`, non-destructive. No bespoke `build_cluster.sh` retry/backoff —
  CAPZ reconciles capacity.
- **"Same image?"** Yes — `microsoft-dsvm:ubuntu-hpc` (HPC-X, OFED, CUDA, NCCL).
  Directly consumes what `hpc-image-val` produces; no separate image needed.
- **"PBS failure-detection (PR 740)?"** Mirrored: `check_slurm_exit_codes` via
  `sacct` catches NODE_FAIL/TIMEOUT/OOM, not just exit code — stronger than PBS.

### Benchmarks / MPI
- **"Why TCP if you have IB?"** Deliberate for the demo — TCP is the proven path.
  Workers are V100 + EDR IB already; flipping `UCX_TLS=tcp`→`rc` for RDMA is the
  next step, not a hardware change. Today: OSU over TCP ~23 µs, ~3.1 GB/s.
- **"NCCL on this?"** Prototyped (`scripts/nccl-slurm/`); runs once RDMA is enabled
  on these 8×V100 nodes. MPI/OSU over TCP works now.
- **"HPC-X getting in?"** Bind-mounted host `/opt/hpcx` for the dev path;
  production = custom slurmd image FROM ubuntu-hpc pushed to ACR (your team's pattern).
- **"InfiniBand?"** Hardware is here (ND40rs_v2, EDR IB). Privileged + hostNetwork
  pods + `/dev/infiniband` (matches your nccl-test.yaml); RDMA enablement in progress.

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
- **"GPU/IB demoed?"** Running on it (2× ND40rs_v2, V100 + EDR IB). Demoing MPI
  over TCP today; RDMA (`UCX_TLS=rc`) + NCCL is the immediate next step.
- **"Pulumi reconciled?"** Self-managed manifest is manual; mentor folds into Pulumi later.
- **"Multi-tenant scale?"** 2-node proof; topology/PPG mostly works through CAPI MachinePool.
