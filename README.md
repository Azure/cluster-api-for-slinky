# Cluster API for Slinky

Kubernetes-native declarative infrastructure for [Slinky](https://github.com/SlinkyProject)-based converged Slurm and Kubernetes clusters.

## What is Cluster API for Slinky (CA4S)

The [Cluster API](https://github.com/kubernetes-sigs/cluster-api) (CAPI) brings declarative, Kubernetes-style APIs to cluster creation, configuration and management.

CA4S enables efficient management at scale of Slinky-based converged Slurm and Kubernetes clusters, with the nodes dual-managed by both Ansible AWX for Slurm workloads and CAPI providers (Cluster API Provider Docker/Azure/vCluster/etc.) for containerized workloads on Kubernetes, with slurm-bridge bridging Slurm and Kubernetes for fair-share scheduling across both orchestrators.

![CA4S architecture](docs/images/architecture.svg)

## Getting started

Currently only local clusters powered by Cluster API Provider Docker (CAPD) is supported.

### Prerequisites

Install the following:

| Tool | Purpose | Install |
|---|---|---|
| [Docker](https://docs.docker.com/engine/install/) | Container runtime hosting both the kind nodes and the CAPD workload-cluster containers | see *Recommended local setup* below |
| [`kind`](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Spins up the management Kubernetes cluster | `go install sigs.k8s.io/kind@latest` |
| [`cloud-provider-kind`](https://github.com/kubernetes-sigs/cloud-provider-kind) | Host-side daemon that makes `type: LoadBalancer` Services on kind reachable from the host (Pulumi spawns/kills it via the `CloudProviderKind` resource) | `go install sigs.k8s.io/cloud-provider-kind@latest` |
| [`ctlptl`](https://github.com/tilt-dev/ctlptl) | Declarative front-end that wraps `kind` to create the management cluster + local image registry (driven by Pulumi, see below) | `go install github.com/tilt-dev/ctlptl/cmd/ctlptl@latest` |
| [`pulumi`](https://www.pulumi.com/docs/install/) | Drives the local bootstrap stack ([`pulumi/`](pulumi/)), which is the supported way to bring up the management cluster, GitOps source, PKO, AWX, and local workload cluster | `curl -fsSL https://get.pulumi.com \| sh` |
| `kubectl`, `helm` | Standard inspection and recovery tooling for the managed local stack | upstream binaries |

### Recommended local setup

| Platform | Recommended | Also supported | Why it matters |
|---|---|---|---|
| **Windows (WSL2)** | **Native Docker inside your WSL2 distro** + WSL2 mirrored networking (`networkingMode=mirrored` in `%USERPROFILE%\.wslconfig` on the Windows host) | — | Native Docker keeps dockerd + kind bridge + your shell in one network namespace, so the LB EXTERNAL-IP is directly curlable. |
| **Linux** | Native Docker (distro package) | — | Just works. |

**Windows (WSL2) one-time setup:**

Two files are involved — `.wslconfig` lives on the **Windows side** (global, applies to all distros), `/etc/wsl.conf` lives **inside the distro** (per-distro):

```powershell
# From PowerShell on Windows — global WSL2 settings (mirrored networking):
@"
[wsl2]
networkingMode=mirrored
"@ | Set-Content -NoNewline "$env:USERPROFILE\.wslconfig"
```

```bash
# Inside your WSL2 distro — per-distro settings (enable systemd):
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true
EOF

# install native Docker
sudo apt install docker.io docker-buildx
sudo usermod -aG docker $USER
```

```powershell
# Then from PowerShell on Windows, restart the WSL2 VM so both files take effect:
wsl --shutdown
# Reopen the distro; systemd will start dockerd. Re-login to pick up docker group.
# Verify:  wslinfo --networking-mode   # should print: mirrored
```

**Linux/WSL2 inotify limits:**

CAPD workload clusters can create enough watches that the default Linux inotify
limits are too low. Persist higher host limits before running the local stack:

```bash
sudo tee /etc/sysctl.d/99-ca4s-inotify.conf >/dev/null <<'EOF'
fs.inotify.max_user_watches=1048576
fs.inotify.max_user_instances=8192
EOF

sudo sysctl --system
sysctl fs.inotify.max_user_watches fs.inotify.max_user_instances
```

On WSL2 with `systemd=true`, `systemd-sysctl` reapplies this file when the
distro starts. If systemd is not enabled, add an equivalent `sysctl -w` command
under `[boot]` in `/etc/wsl.conf`.

**Docker Hub mirror:**

Local runs pull Docker Hub images through two different paths. The
Pulumi-managed ctlptl registry is used by containerd inside the kind and CAPD
nodes, but it is not a mirror for the host Docker daemon itself. Host-side pulls
therefore still need Docker's own mirror configuration: the initial
`kindest/node` management-node images, CAPD DockerMachine node images, and the
`envoyproxy/envoy` image launched internally by `cloud-provider-kind` are pulled
by host Docker, bypassing the ctlptl registry. Configure the host Docker daemon
with Google's Docker Hub mirror:

```bash
sudo mkdir -p /etc/docker
if [ -f /etc/docker/daemon.json ]; then
  sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.bak.$(date +%Y%m%d%H%M%S)
else
  echo '{}' | sudo tee /etc/docker/daemon.json >/dev/null
fi

jq '. + {"registry-mirrors": (((."registry-mirrors" // []) + ["https://mirror.gcr.io"]) | unique)}' \
  /etc/docker/daemon.json | sudo tee /etc/docker/daemon.json.tmp >/dev/null
sudo mv /etc/docker/daemon.json.tmp /etc/docker/daemon.json
sudo systemctl restart docker
docker info --format '{{json .RegistryConfig.Mirrors}}'
```

The management kind cluster routes in-cluster `docker.io` pulls through the
ctlptl registry cache, which is backed by `mirror.gcr.io`; this is the path used
for Docker Hub pod images such as the Pulumi Kubernetes Operator and PKO
workspace image. CAPD workload nodes are bootstrapped to use the same local
registry cache through its host-published port.

### Local Python environment (Pulumi + tests)

The umbrella stack and its test suite share a single virtualenv at the repo root. [`pulumi/Pulumi.yaml`](pulumi/Pulumi.yaml) pins `runtime.options.virtualenv` to `../.venv`, so `pulumi up` and `pytest` resolve the same Pulumi SDKs, generated provider SDKs, and test libraries out of [`requirements.txt`](requirements.txt). One-time setup:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Running the tests (unit tier, fast, no Docker required):

```bash
cd pulumi
../.venv/bin/python -m pytest
```

The pytest config lives in [`pulumi/pyproject.toml`](pulumi/pyproject.toml). Default `addopts` skip the `integration`-marked end-to-end suite that drives a real `pulumi up`/`destroy` cycle; opt in with `../.venv/bin/python -m pytest -m integration` once you have Docker running and ~2 min to spare.

### Bringing up the local stack

The repo ships a single umbrella Pulumi program under [`pulumi/`](pulumi/). It
creates the local developer environment in dependency order:

1. A local image registry and management kind cluster through ctlptl.
2. A `cloud-provider-kind` daemon so `LoadBalancer` Services are reachable from
   the host.
3. In-cluster Gitea plus a seeded GitOps repository containing the current local
   `HEAD`.
4. Flux source wiring and a PKO `Stack` that runs the inner `ca4s-init` program.
5. Control-plane services: CAPI Operator, cert-manager, AWX Operator, AWX
   instance, and AWX API configuration.
6. A local CAPD workload cluster with local-path storage, Calico, workload
    cert-manager, kube-prometheus-stack, KEDA, Slinky operator, and Slurm.

The old root-level AWX, CAPI quickstart, Helm values, and setup manifests have
been removed. The supported local path is Pulumi/PKO.

```bash
pushd pulumi
pulumi stack init local        # first time only; creates Pulumi.local.yaml
pulumi up -s local --yes
popd
```

The outer `pulumi up` waits for the PKO init stack to finish reconciliation.

Optional troubleshooting snippets:

```bash
# CONTEXT=$(pulumi stack output context -s local)
# PKO_INIT_STACK=$(pulumi stack output pko_init_stack -s local)
# kubectl --context "$CONTEXT" -n pulumi-kubernetes-operator get stack "$PKO_INIT_STACK" -w

# REGISTRY_PORT=$(pulumi stack output registry_port -s local)
# kubectl cluster-info --context "$CONTEXT"
# echo "local registry at localhost:${REGISTRY_PORT}"

# pulumi stack output gitops_url -s local
# pulumi stack output gitops_url_external -s local
# pulumi stack output pko_flux_source_name -s local

# request="manual-$(date +%s)"
# kubectl --context "$CONTEXT" -n pulumi-kubernetes-operator annotate gitrepository gitops-source \
#   reconcile.fluxcd.io/requestedAt="$request" --overwrite
# kubectl --context "$CONTEXT" -n pulumi-kubernetes-operator wait gitrepository/gitops-source \
#   --for=jsonpath='{.status.lastHandledReconcileAt}'="$request" --timeout=180s

# request="manual-$(date +%s)"
# kubectl --context "$CONTEXT" -n pulumi-kubernetes-operator annotate stack "$PKO_INIT_STACK" \
#   pulumi.com/reconciliation-request="$request" --overwrite
```

### Workload Cluster And Slurm

Extract the CAPD workload-cluster kubeconfig from the management cluster:

```bash
# set passphrase with PULUMI_CONFIG_PASSPHRASE or PULUMI_CONFIG_PASSPHRASE_FILE first
MGMT_CONTEXT=$(pulumi -C pulumi stack output context -s local)
WORKLOAD_CLUSTER=$(kubectl --context "$MGMT_CONTEXT" -n default \
  get cluster -o jsonpath='{.items[0].metadata.name}')
WORKLOAD_KUBECONFIG="$PWD/${WORKLOAD_CLUSTER}.kubeconfig"

kubectl --context "$MGMT_CONTEXT" -n default \
  get secret "${WORKLOAD_CLUSTER}-kubeconfig" \
  -o jsonpath='{.data.value}' | base64 -d > "$WORKLOAD_KUBECONFIG"
```

Find the Slurm login pod and verify access:

```bash
LOGIN_POD=$(kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n slurm \
  get pod -l app.kubernetes.io/component=login,app.kubernetes.io/name=login,app.kubernetes.io/part-of=slurm \
  -o jsonpath='{.items[0].metadata.name}')
kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n slurm exec -it "$LOGIN_POD" -- sinfo
```

### Reuse The Host Resource Group

AKS and Azure BYO workload parameters accept `useDiscoveredResourceGroup`:

```yaml
tenants:
  workloadClusters:
    caps-self:
      className: azure-byo # or aks
      parameters:
        useDiscoveredResourceGroup: true
```

When enabled, host-side Azure IMDS discovery selects the resource group that
contains the VM running Docker and the Kind management cluster. The discovered
resource group must belong to the workload subscription, but its own location
does not constrain the locations of resources placed in it. Azure BYO then
references that existing group instead of registering a new Pulumi-owned resource
group. AKS uses it as the managed cluster resource group; Azure still creates and
manages the separate `MC_*` node resource group.

CAPZ treats an untagged pre-existing resource group as unmanaged: deleting the
workload cluster deletes its cluster resources individually but preserves the
host resource group and Kind host VM. Do not apply CAPZ's cluster ownership tag
to the shared group.

### Autoscaling

See [docs/autoscaling.md](docs/autoscaling.md) for the autoscaling design,
including NodeSet placement, Cluster Autoscaler ownership, and scale-in behavior.

Run the manual demand generator through the login pod:

```bash
kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n slurm cp scripts/sleep-exclusive.slurm "$LOGIN_POD:/root/sleep-exclusive.slurm"
kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n slurm cp scripts/slurm_load_generator.py "$LOGIN_POD:/root/slurm_load_generator.py"
kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n slurm exec "$LOGIN_POD" -- chmod +x /root/slurm_load_generator.py
LOAD_PID=$(kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n slurm exec "$LOGIN_POD" -- \
  sh -lc 'nohup /root/slurm_load_generator.py --profile azure --job-script /root/sleep-exclusive.slurm >/root/slurm_load_generator.log 2>&1 & echo $!')
```

`--profile` provides environment-specific defaults:

- `local`: `MIN_RATE=1`, `MAX_RATE=5`, `CYCLE_MINUTES=16`.
- `azure` or `cloud`: `MIN_RATE=0.25`, `MAX_RATE=4`, `CYCLE_MINUTES=120`.

`MIN_RATE` and `MAX_RATE` are jobs per minute and can be fractional. CLI flags
such as `--min-rate`, `--max-rate`, and `--cycle-minutes` override environment
variables, and environment variables such as `LOAD_PROFILE` and `MIN_RATE`
override profile defaults.

The generator projects angular phase through a sinusoidal wave and
integrates that instantaneous jobs-per-minute frequency every logical second.
Use `--tick-seconds` to change the integration interval and `--minute-seconds`
to accelerate wall-clock time without changing the logical load profile.

Watch Slurm nodes come and go:

```bash
watch -n 10 "kubectl --kubeconfig '$WORKLOAD_KUBECONFIG' -n slurm exec '$LOGIN_POD' -- sinfo"
```

Port-forward Grafana and print its admin credentials:

```bash
GRAFANA_SVC=$(kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n prometheus \
  get svc -l app.kubernetes.io/name=grafana \
  -o jsonpath='{.items[0].metadata.name}')
GRAFANA_SECRET=$(kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n prometheus \
  get secret -l app.kubernetes.io/name=grafana \
  -o jsonpath='{.items[0].metadata.name}')

printf 'username: '
kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n prometheus \
  get secret "$GRAFANA_SECRET" -o jsonpath='{.data.admin-user}' | base64 -d ; echo
printf 'password: '
kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n prometheus \
  get secret "$GRAFANA_SECRET" -o jsonpath='{.data.admin-password}' | base64 -d ; echo

kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n prometheus \
  port-forward "svc/$GRAFANA_SVC" 3000:80
```

Then browse to `http://localhost:3000`.

Stop the load generator after the watch:

```bash
kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n slurm exec "$LOGIN_POD" -- kill "$LOAD_PID"
```

## Components

The local stack is split into a few cooperating components:

- **ctlptl registry and management kind cluster**: ctlptl creates the local
  registry and the management kind cluster. The management cluster hosts the
  control-plane services and the CAPI resources that describe workload clusters.
- **cloud-provider-kind**: a host-side daemon that gives kind `LoadBalancer`
  Services reachable addresses, including the Gitea and AWX Services used during
  local development.
- **Gitea and Flux**: Gitea is the in-cluster GitOps source seeded from local
  `HEAD`. Flux watches that repository and produces source artifacts for PKO.
- **Pulumi Kubernetes Operator (PKO)**: PKO runs the inner Pulumi stacks from
  Flux artifacts. The outer stack owns the initial `ca4s-init` Stack CR; that
  inner stack owns the control-plane and workload-cluster components.
- **Cluster API and CAPD**: CAPI models the workload cluster in the management
  cluster. CAPD turns those CAPI resources into local Docker-backed Kubernetes
  nodes.
- **Workload services**: the workload cluster installs Calico, local-path
  storage, cert-manager, kube-prometheus-stack, KEDA, the Slinky operator, and
  Slurm.
- **AWX**: AWX is managed by the control-plane stack. Pulumi creates the AWX
  organization, GitOps source-control credential, project-sync fence, management
  Kubernetes credential type, dynamic CAPI/Slinky inventory source, and the
  `ca4s-collect-cluster-state` job template.

To inspect AWX locally:

```bash
kubectl -n awx port-forward svc/awx-service 8080:80
kubectl -n awx get secret awx-admin-password \
  -o jsonpath='{.data.password}' | base64 -d ; echo
```

Then browse to `http://localhost:8080` and log in as `admin`.

## Contributing

This project welcomes contributions and suggestions.  Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit https://cla.opensource.microsoft.com.

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

Slurm® and Slinky® are registered trademarks of SchedMD LLC.

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft 
trademarks or logos is subject to and must follow 
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
