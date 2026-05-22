# Cluster API Provider Slinky

Kubernetes-native declarative infrastructure for [Slinky](https://github.com/SlinkyProject)-based converged Slurm and Kubernetes clusters.

## What is the Cluster API Provider Slinky (CAPS)

The [Cluster API](https://github.com/kubernetes-sigs/cluster-api) (CAPI) brings declarative, Kubernetes-style APIs to cluster creation, configuration and management.

CAPS enables efficient management at scale of Slinky-based converged Slurm and Kubernetes clusters, with the nodes dual-managed by both Ansible AWX for Slurm workloads and CAPI providers (Cluster API Provider Docker/Azure/vCluster/etc.) for containerized workloads on Kubernetes, with slurm-bridge bridging Slurm and Kubernetes for fair-share scheduling across both orchestrators.

![CAPS architecture](docs/images/architecture.svg)

## Getting started

Currently only local clusters powered by Cluster API Provider Docker (CAPD) is supported.

### Recommended local setup

| Platform | Recommended | Also supported | Why it matters |
|---|---|---|---|
| **Windows (WSL2)** | **Native Docker inside your WSL2 distro** + WSL2 mirrored networking (`networkingMode=mirrored` in `%USERPROFILE%\.wslconfig` on the Windows host) | Docker Desktop with WSL2 integration | Native Docker keeps dockerd + kind bridge + your shell in one network namespace, so the LB EXTERNAL-IP is directly curlable. Docker Desktop runs dockerd in a separate `docker-desktop` distro and isolates the bridge — `localhost:<port>` works, but EXTERNAL-IP does not. |
| **Linux** | Native Docker (distro package) | — | Just works. |
| **macOS** | **OrbStack** (proxies container subnets back to the host) | Docker Desktop, Colima, Rancher Desktop, Podman Machine | OrbStack makes LB EXTERNAL-IPs directly curlable from the Mac shell; the others restrict you to `localhost:<port>`. Note: validation of the Gitea hydration path on each Mac runtime is still pending (see [TODOs](#todo)). |

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

# install native Docker (replaces Docker Desktop if you had it integrated)
sudo apt install docker.io docker-buildx
sudo usermod -aG docker $USER
```

```powershell
# Then from PowerShell on Windows, restart the WSL2 VM so both files take effect:
wsl --shutdown
# Reopen the distro; systemd will start dockerd. Re-login to pick up docker group.
# Verify:  wslinfo --networking-mode   # should print: mirrored
```

### Prerequisites

Install the following:

| Tool | Purpose | Install |
|---|---|---|
| [Docker](https://docs.docker.com/engine/install/) | Container runtime hosting both the kind nodes and the CAPD workload-cluster containers | see *Recommended local setup* above |
| [`kind`](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Spins up the management Kubernetes cluster | `go install sigs.k8s.io/kind@latest` |
| [`cloud-provider-kind`](https://github.com/kubernetes-sigs/cloud-provider-kind) | Host-side daemon that makes `type: LoadBalancer` Services on kind reachable from the host (Pulumi spawns/kills it via the `CloudProviderKind` resource) | `go install sigs.k8s.io/cloud-provider-kind@latest` |
| [`ctlptl`](https://github.com/tilt-dev/ctlptl) | Declarative front-end that wraps `kind` to create the management cluster + local image registry (driven by Pulumi, see below) | `go install github.com/tilt-dev/ctlptl/cmd/ctlptl@latest` |
| [`pulumi`](https://www.pulumi.com/docs/install/) | Drives the local bootstrap stack ([`pulumi/`](pulumi/)), which is the sole supported way to bring up the management cluster + Gitea + (eventually) Flux | `curl -fsSL https://get.pulumi.com \| sh` |
| `kubectl`, `helm`, `clusterctl` | Standard tooling for the post-bootstrap steps below | upstream binaries |

<a id="todo"></a>

#### TODOs (macOS Gitea-hydration path)

The GitOps phase of the bootstrap stack seeds Gitea via a one-time
`git push` from the Pulumi host. Before declaring macOS supported:

- Validate the `kubectl port-forward` + `git push http://localhost:<port>/…` path on **Docker Desktop**, **Colima**, **Rancher Desktop**, **OrbStack**.
- Decide whether `GiteaSeed` should special-case OrbStack to skip port-forward (uses direct EXTERNAL-IP for speed) or always use port-forward for consistency.
- Add a Mac-equivalent of the Linux `setcap` preflight in `CloudProviderKind` — likely a `run_as_root: bool` input that wraps `argv` in `sudo -n` and `delete()` in `sudo -n kill`. Only needed if a Mac user wants bridge-IP mode; the default port-mapping mode does not need it.

### Bringing up the management cluster (+ Gitea)

The repo ships a single umbrella Pulumi program under [`pulumi/`](pulumi/) that brings up everything the developer loop needs in dependency order. Phase 1 — cluster + registry + LB controller:

1. Creates a local image registry (`CtlptlRegistry`) on an ephemeral host port that ctlptl picks via [freeport](https://github.com/phayes/freeport) and persists in Pulumi state.
2. Creates the management kind cluster (`CtlptlCluster`) from a vendored manifest, wired to the registry created in step 1 and to your `$HOME/.kube` for in-cluster kubeconfig mounts.
3. Spawns the [`cloud-provider-kind`](https://github.com/kubernetes-sigs/cloud-provider-kind) daemon (`CloudProviderKind`) as a detached host process so `type: LoadBalancer` Services on kind get real `127.0.0.1:<port>` IPs. PID + log file are persisted in Pulumi state; the daemon is killed on `pulumi destroy`.

The ctlptl manifest is vendored inside the Pulumi program ([`pulumi/ctlptl/ctlptl_cluster.py`](pulumi/ctlptl/ctlptl_cluster.py)); there is no on-disk YAML to edit. The legacy [`ctlptl.yaml`](ctlptl.yaml) and [`setup.sh`](setup.sh) at the repo root are **deprecated** and only kept as historical references.

Phase 2 — GitOps source (in-cluster Gitea by default; see [`pulumi/gitrepo/`](pulumi/gitrepo/)):

1. Installs Gitea via the upstream Helm chart (pinned in [`gitea_builtin.py`](pulumi/gitrepo/gitea_builtin.py)) into the management cluster — sqlite + in-memory cache, single 2 GiB PVC backing `/data` so the seeded repo survives pod restarts.
2. Generates a random admin password, stores it as `gitea-credentials` (`username` / `password` keys) in the `gitea` namespace — same Secret consumed by the Gitea chart's `existingSecret` and by downstream Flux `GitRepository.spec.secretRef`.
3. Exposes Gitea's HTTP service as `type: LoadBalancer` so `cloud-provider-kind` publishes a host-reachable address.
4. Creates the empty `<admin>/cluster-api-provider-slinky` repository via Gitea's REST API ([`gitea_repo.py`](pulumi/gitrepo/gitea_repo.py)).
5. Force-pushes the local working tree's current `HEAD` into that repo's default branch ([`gitea_seed.py`](pulumi/gitrepo/gitea_seed.py)) — the seed re-runs only when local `HEAD` moves.

Phase 3 — Flux bootstrap: *not yet implemented*; tracked as a TODO block in [`pulumi/__main__.py`](pulumi/__main__.py).

```bash
cd pulumi
pulumi up
# Phase 1 outputs:
CONTEXT=$(pulumi stack output context)             # e.g. kind-mgmt-<hash>
REGISTRY_PORT=$(pulumi stack output registry_port) # e.g. 43181
kubectl cluster-info --context "$CONTEXT"
echo "local registry at localhost:${REGISTRY_PORT}"

# Phase 2 outputs (GitOpsRepository contract — consumed by Flux):
pulumi stack output gitops_url                          # in-cluster URL for Flux GitRepository.spec.url
pulumi stack output gitops_url_external                 # host-reachable URL, e.g. http://172.18.0.5:3000/...
pulumi stack output gitops_default_branch               # main
pulumi stack output gitops_credentials_secret_name      # gitea-credentials
pulumi stack output gitops_credentials_secret_namespace # gitea
```

The admin password is stored encrypted in Pulumi state; surface it for AWX / browser use with:

```bash
kubectl -n gitea get secret gitea-credentials -o jsonpath='{.data.password}' | base64 -d ; echo
```

Install AWX Operator.

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
helm repo add awx-operator https://ansible-community.github.io/awx-operator-helm/
helm install my-awx-operator awx-operator/awx-operator -n awx --create-namespace -f awx.yaml
# display your AWX admin password
kubectl get secret awx-admin-password -n awx -o jsonpath="{.data.password}" | base64 --decode ; echo
```

Using your AWX `admin` username and password, log in to AWX. Forward the AWX service to a local port (the kind cluster no longer publishes 32000 on the host — we use just-in-time port forwarding instead to avoid conflicts):

```bash
kubectl -n awx port-forward svc/awx-service 8080:80
```

Then browse to `http://localhost:8080`.

Go to `Administration -> Instance Groups -> default`, press `Edit`, then check `Customize pod specification` and paste `pod-spec-override.yaml` into the `Custom pod spec` field, in order for the kubeconfig directory to properly mount into AWX runners, so that AWX can access data of management cluster. (TODO: instead of this kubeconfig mounting hack, properly introduce k8s credentials into AWX via awx-operator or some other methods)

Go to `Resources -> Credentials` and add your Gitea username and password as a credential of type `Source Control`.

Go to `Resources -> Projects` and add a project of Source Control Type `Git`. The Source Control URL should be `http://host.docker.internal:3000/<your gitea admin username>/cluster-api-provider-slinky.git`, with appropriate branch/tag/commit name and Source Control Credential pointing to the credential you added in the last step.

Go to `Resources -> Inventories` and add an inventory. Go to `Sources` of this inventory, add an inventory source `Sourced from a project`, and choose the Project you added in the last step, with Inventory file set as `projects/test/roles/sync/files/run.py`. For Update options, select `Overwrite` and `Update on launch`.

Go to `Resources -> Credentials` and add another credential of type `Machine`, with `root` as username and containing your SSH private key (and passphrase if you have any). This SSH Key would be used by Ansible to access pseudo-nodes bootstrapped by CAPD.

Go to `Resources -> Templates` and add a job template, with Job Type of `Run`, Inventory and project set to what you created in previous steps, Playbook set to `projects/test/run.yml`, and Credentials set to the previous credential. This is a sample Ansible job that collects hostnames of nodes in the inventory, which will trigger the refresh of the inventory (which collects Cluster API node data) every time it runs.

Install [CAPD](https://github.com/kubernetes-sigs/cluster-api/blob/main/test/infrastructure/docker/README.md) and turn your current Kind cluster into a CAPD management cluster, which we will use to create and manage CAPD workload clusters in the steps after.
```bash
export CLUSTER_TOPOLOGY=true
clusterctl init --infrastructure docker
# enhance metadata propagation so that Slinky labels will land on the node (for MachineSet and Machine only; CAPD MachinePool label seems to propagate just fine without this trick)
kubectl -n capi-system patch deployment/capi-controller-manager --type=json -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--additional-sync-machine-labels=.*slinky\\.slurm\\.net.*"}]'
```

open `capi-quickstart.yaml` and modify the SSH public key (corresponding to the private key in AWX) inside `preKubeCommands` (around line 329).

Create the CAPD cluster. wait for the cluster to be ready.
```bash
kubectl apply -f capi-quickstart.yaml
# wait some time
clusterctl get kubeconfig capi-quickstart > capi-quickstart.kubeconfig
# https://cluster-api.sigs.k8s.io/clusterctl/developers#fix-kubeconfig-when-using-docker-desktop-and-clusterctl
# Point the kubeconfig to the exposed port of the load balancer, rather than the inaccessible container IP.
sed -i -e "s/server:.*/server: https:\/\/$(docker port capi-quickstart-lb 6443/tcp | sed "s/0.0.0.0/host.docker.internal/")/g" ./capi-quickstart.kubeconfig
# CAPD nodes won't be ready until we install CNI
# Download Calico manifest and replace docker.io images with quay.io to avoid Docker Hub rate limits
curl -sL https://raw.githubusercontent.com/projectcalico/calico/v3.30.3/manifests/calico.yaml \
  | sed 's|docker.io/calico/|quay.io/calico/|g' \
  | kubectl --kubeconfig=./capi-quickstart.kubeconfig apply -f -
# monitor CAPD cluster and wait for it to become ready
watch -c clusterctl describe cluster capi-quickstart --color
```

Run the job in AWX. The job should now output the hostnames of all pseudo-nodes of the CAPD cluster (which are Docker containers under the hood), and the inventory's host list should also contain those hosts. This means that AWX is now aware of these nodes and Ansible can bootstrap SSSD/Storage/networking/etc.

Next, we introduce Slurm into the cluster by installing `slurm-operator`. We need to switch to the CAPD cluster's k8s context first.
```bash
export KUBECONFIG="$(pwd)/capi-quickstart.kubeconfig"
# add a local-path-based StorageClass for Slurm persistence
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.32/deploy/local-path-storage.yaml
kubectl patch storageclass local-path -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
kubectl patch storageclass local-path -p '{"metadata":{"annotations":{"defaultVolumeType":"local"}}}'
# relax pod security for local storage to work
kubectl label ns local-path-storage pod-security.kubernetes.io/enforce=privileged --overwrite
kubectl label ns local-path-storage pod-security.kubernetes.io/enforce-version=latest --overwrite
# Pin platform Deployments installed as raw manifests (calico-kube-controllers, coredns,
# local-path-provisioner) to the controller node, so cluster-autoscaler can scale compute in.
./scripts/pin-platform-pods.sh
helm install cert-manager oci://quay.io/jetstack/charts/cert-manager --version v1.18.2 -f cert-manager-values.yaml --namespace cert-manager --create-namespace
helm install slurm-operator-crds oci://ghcr.io/slinkyproject/charts/slurm-operator-crds
helm install slurm-operator oci://ghcr.io/slinkyproject/charts/slurm-operator -f slurm-operator-values.yaml --namespace=slinky --create-namespace
helm install slurm oci://ghcr.io/slinkyproject/charts/slurm -f slurm-cluster.yaml --set-file "loginsets.slinky.rootSshAuthorizedKeys=${HOME}/.ssh/id_rsa.pub" --namespace=slurm --create-namespace
kubectl label ns slurm pod-security.kubernetes.io/enforce=privileged --overwrite
kubectl label ns slurm pod-security.kubernetes.io/enforce-version=latest --overwrite
```

Next, we set up Prometheus/Grafana on the workload cluster:
```bash
# switch to CAPI workload cluster again
export KUBECONFIG="$(pwd)/capi-quickstart.kubeconfig"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
# All non-DaemonSet prometheus components are pinned to controller via prometheus-values.yaml;
# prometheus-node-exporter (DaemonSet) is intentionally left to run on every node.
helm install prometheus prometheus-community/kube-prometheus-stack -f prometheus-values.yaml --namespace=prometheus --create-namespace
kubectl label ns prometheus pod-security.kubernetes.io/enforce=privileged --overwrite
kubectl label ns prometheus pod-security.kubernetes.io/enforce-version=latest --overwrite
# check status
kubectl --namespace prometheus get pods -l "release=prometheus"
# get Grafana 'admin' user password
kubectl --namespace prometheus get secrets prometheus-grafana -o jsonpath="{.data.admin-password}" | base64 -d ; echo
# access Grafana local instance
export POD_NAME=$(kubectl --namespace prometheus get pod -l "app.kubernetes.io/name=grafana,app.kubernetes.io/instance=prometheus" -oname)
kubectl --namespace prometheus port-forward $POD_NAME 3000
```

In a separate terminal, port forward the Slurm login node:
```bash
kubectl -n slurm port-forward svc/slurm-login-slinky 2222:22
```

Log into the Slurm login node:
```bash
ssh -p 2222 root@127.0.0.1
```

Run Slurm commands to quickly verify that Slurm is functioning:
```bash
sinfo
srun hostname
sbatch --wrap="sleep 60"
squeue
sacct
```

## Platform pod placement

Compute nodes are tainted with `slinky.slurm.net/controller:NoSchedule` on the controller MachineDeployment only; the MachinePool used for compute is untainted so general Kubernetes workloads can land freely. Slurm NodeSet pods reach compute nodes via `nodeAffinity` (no toleration needed).

Every other platform component (`cert-manager`, `slurm-operator`, `kube-prometheus-stack`, `keda`, `local-path-provisioner`, `coredns`, `calico-kube-controllers`) is pinned to the controller node so that **no non-DaemonSet pod ever lands on a compute node and blocks cluster-autoscaler scale-in**. DaemonSets (`calico-node`, `kube-proxy`, `prometheus-node-exporter`) intentionally run everywhere — they don't block scale-in.

| File | Used by |
|---|---|
| `cert-manager-values.yaml` | `helm install cert-manager -f cert-manager-values.yaml` |
| `slurm-operator-values.yaml` | `helm install slurm-operator -f slurm-operator-values.yaml` |
| `slurm-cluster.yaml` | `helm install slurm -f slurm-cluster.yaml` (head pods + NodeSet affinity) |
| `prometheus-values.yaml` | `helm install prometheus -f prometheus-values.yaml` |
| `keda-values.yaml` | `helm install keda -f keda-values.yaml` |
| `scripts/pin-platform-pods.sh` | Post-install patches for raw-manifest Deployments (coredns, calico-kube-controllers, local-path-provisioner) |

## Locally building Slinky

<!--
TODO(host-registry-port): the helm-oci pulls below hardcode
`host.docker.internal:5000`. The registry's host port is now allocated at
runtime by Pulumi (see `pulumi -C pulumi stack output
registry_port`), so these commands will fail until callers either:
  (a) read the chosen port from the stack output and substitute it in, or
  (b) reach the registry by its container name on the kind network
      (`pulumi -C pulumi stack output registry_name`, port 5000)
      from inside the cluster.
The matching containerd mirror in capi-quickstart.yaml is also stale; see
the TODO in scripts/generate_capi_quickstart.py.
-->

```bash
# Local container registry for Slinky development, created by the Pulumi
# bootstrap stack (pulumi/). Read its host-side address with:
#   pulumi -C pulumi stack output registry_port
# and its in-cluster container name with:
#   pulumi -C pulumi stack output registry_name
# in another slurm-operator repo, run `make push REGISTRY=host.docker.internal:5000/slinky`, then we switch to CAPD cluster and install:
helm install slurm-operator-crds oci://host.docker.internal:5000/slinky/charts/slurm-operator-crds
helm install slurm-operator oci://host.docker.internal:5000/slinky/charts/slurm-operator -f slurm-operator-values.yaml --namespace=slinky --create-namespace
helm install slurm oci://host.docker.internal:5000/slinky/charts/slurm -f slurm-cluster.yaml --set-file "loginsets.slinky.rootSshAuthorizedKeys=${HOME}/.ssh/id_rsa.pub" --namespace=slurm --create-namespace
kubectl label ns slurm pod-security.kubernetes.io/enforce=privileged --overwrite
kubectl label ns slurm pod-security.kubernetes.io/enforce-version=latest --overwrite
```

## Autoscaling

The autoscaling logical workflow behaves as follows:
- Slurm jobs are submitted
- slurmctld built-in metrics endpoint exports Slurm job queue data into Prometheus
- Keda (or some other more sophisticated custom logic) scales Slinky NodeSet replicas based on Prometheus data
- more NodeSet replicas lead to unschedulable NodeSet pods
- Cluster Autoscaler sees unschedulable pods, and Cluster API cloud provider of Cluster Autoscaler scales up the MachinePool
- MachinePool increases its number of replicas, which brings more nodes into the workload cluster
- NodeSet pods is now schedulable onto the newly-introduced nodes
- new nodes join the Slurm cluster and pick up the jobs

To set up the autoscaling configuration, we first connect the Cluster Autoscaler to Cluster API. For our CAPD setup,
```bash
# Switch your kubectl context/kubeconfig to the management cluster first
unset KUBECONFIG # go back to Kind's kubeconfig
clusterctl get kubeconfig capi-quickstart > ${HOME}/.kube/capi-quickstart.kubeconfig # this time we actually want the in-management-cluster load-balancer IP!
helm repo add autoscaler https://kubernetes.github.io/autoscaler
helm install cluster-autoscaler autoscaler/cluster-autoscaler -f cluster-autoscaler.yaml --namespace=cluster-autoscaler --create-namespace
kubectl label ns cluster-autoscaler pod-security.kubernetes.io/enforce=privileged --overwrite
kubectl label ns cluster-autoscaler pod-security.kubernetes.io/enforce-version=latest --overwrite
# Extra RBAC so the autoscaler can access CAPD infrastructure resources
kubectl apply -f cluster-autoscaler-capd-rbac.yaml
```

We then install KEDA to scale the Slurm NodeSet based on Prometheus metrics of pending jobs:
```bash
# Switch to CAPI workload cluster again
export KUBECONFIG="$(pwd)/capi-quickstart.kubeconfig"
helm repo add kedacore https://kedacore.github.io/charts
helm repo update
helm install keda kedacore/keda -f keda-values.yaml --namespace keda --create-namespace
kubectl apply -f nodeset-scaledobject.yaml
```

Finally, with login node still port forwarded to port 2222, let's do some autoscale testing:
```bash
scp -P 2222 scripts/sleep-exclusive.slurm root@127.0.0.1:~/
scp -P 2222 scripts/slurm_load_generator.sh root@127.0.0.1:~/
ssh -p 2222 root@127.0.0.1 'chmod +x slurm_load_generator.sh'
ssh -p 2222 root@127.0.0.1 './slurm_load_generator.sh'
```

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
