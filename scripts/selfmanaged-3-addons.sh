#!/usr/bin/env bash
#
# selfmanaged-3-addons.sh - install the Day-2 addons on the self-managed workload
# cluster `caps-self`, so its nodes leave NotReady.
#
# Runs ON the CAPZ management VM (needs kubectl + line-of-sight to the workload
# API server). Drive it from a workstation with:
#     scripts/azure-remote.sh addons
#
# Why these two, in this order:
#   1. cloud-provider-azure (CCM + cloud-node-manager) - the cluster runs with
#      cloud-provider=external, so every node boots tainted
#      `node.cloudprovider.kubernetes.io/uninitialized:NoSchedule`. The CCM reads
#      the host `/etc/kubernetes/azure.json` (UAMI, no secrets) that the kubeadm
#      files mount, stamps each node's providerID/addresses, and clears that
#      taint. Installed via the upstream Helm chart (the out-of-tree raw
#      manifests still target the removed `node-role.kubernetes.io/master`
#      label/taint and expect azure.json creds in a Secret - both wrong here).
#   2. Calico CNI - pod networking, so nodes report Ready and CoreDNS schedules.
#      Pod CIDR 192.168.0.0/16 already matches Calico's default IPAM pool. Images
#      are rewritten docker.io -> quay.io to dodge Docker Hub rate limits (same
#      trick the CAPD path in the README uses).
#
# PREREQUISITE (networking): the control-plane subnet must have OUTBOUND internet
# (a NAT gateway) or these images cannot be pulled — the CP node has no public IP
# and the internal API LB gives it no egress, so a bare CP subnet leaves every
# addon stuck ImagePullBackOff. The manifest now declares this on the CP subnet
# (networkSpec...caps-self-cp-subnet.natGateway.name: caps-self-cp-natgw). NOTE:
# on this BYO VNet, CAPZ references that NAT gateway by name but does NOT create
# it, so pre-create it once (matching CAPZ's pip-<natgw> naming) and CAPZ keeps
# the association:
#   az network public-ip create -g rg-capz-mi-dev2 -n pip-caps-self-cp-natgw --sku Standard -l westus2 --tags Owner=<you>
#   az network nat gateway create -g rg-capz-mi-dev2 -n caps-self-cp-natgw --public-ip-addresses pip-caps-self-cp-natgw -l westus2 --tags Owner=<you>
#   az network vnet subnet update -g rg-capz-mi-dev2 --vnet-name vm-capz-mi-devVNET -n caps-self-cp-subnet --nat-gateway caps-self-cp-natgw
# A manual association WITHOUT the matching manifest entry gets reconciled away.
# (Only the k8s control-plane images are pre-baked into the CAPI gallery image;
# Calico/CCM are not, hence the hard requirement.)
#
# Idempotent: re-running upgrades the chart in place and re-applies Calico.
set -euo pipefail

CLUSTER="${CLUSTER:-caps-self}"
NAMESPACE="${NAMESPACE:-default}"
POD_CIDR="${POD_CIDR:-192.168.0.0/16}"
CALICO_VERSION="${CALICO_VERSION:-v3.30.3}"
CPA_HELM_REPO="https://raw.githubusercontent.com/kubernetes-sigs/cloud-provider-azure/master/helm/repo"

CTX="$(kubectl config current-context)"
echo ">> mgmt context: $CTX"

# --- workload kubeconfig --------------------------------------------------------
# clusterctl isn't installed on the VM, so pull the kubeconfig straight from the
# CAPI-managed secret instead of `clusterctl get kubeconfig`.
WORKLOAD_KCFG="$(mktemp "/tmp/${CLUSTER}.kubeconfig.XXXXXX")"
trap 'rm -f "$WORKLOAD_KCFG"' EXIT
kubectl --context "$CTX" -n "$NAMESPACE" get secret "${CLUSTER}-kubeconfig" \
  -o jsonpath='{.data.value}' | base64 -d > "$WORKLOAD_KCFG"
echo ">> workload kubeconfig: $WORKLOAD_KCFG"
echo ">> nodes before:"
kubectl --kubeconfig "$WORKLOAD_KCFG" get nodes -o wide || true

# --- 0. kube-proxy apiserver reachability (single-CP + INTERNAL API LB only) -----
# With an internal API-server LB and ONE control-plane replica, that CP node is
# itself the LB backend, and Azure Standard LB forbids a backend reaching the
# frontend VIP it belongs to (hairpin). kube-proxy's kubeconfig points at the LB
# FQDN, so it can't reach the apiserver, never syncs, and never programs the
# ClusterIP rule for 10.96.0.1 -> the apiserver. Result: CCM/CoreDNS/Calico all
# time out on the in-cluster API. Fix: pin the apiserver FQDN to the CP node's
# own IP via kube-proxy hostAliases (kubelet honors hostAliases even for
# hostNetwork pods). Single-CP only; revisit for an HA control plane.
APISERVER_HOST="$(kubectl --context "$CTX" -n "$NAMESPACE" get azurecluster "$CLUSTER" -o jsonpath='{.spec.controlPlaneEndpoint.host}' 2>/dev/null || true)"
# Scope BOTH labels to THIS cluster: `cluster.x-k8s.io/control-plane` alone is
# cluster-agnostic, so with more than one workload cluster in the namespace
# `.items[0]` can return a DIFFERENT cluster's CP IP (e.g. caps-self's 10.1.1.4
# instead of caps-val's 10.2.1.4), mis-pinning kube-proxy's apiserver hostAlias.
# RETRY: the CP Machine's status InternalIP can lag a few seconds behind the
# control plane reporting Initialized (CAPZ populates addresses asynchronously),
# so poll rather than skip — else the single-CP hairpin fix is missed and the CP
# node never goes Ready.
CP_IP=""
for _ in $(seq 1 20); do
  CP_IP="$(kubectl --context "$CTX" -n "$NAMESPACE" get machine -l cluster.x-k8s.io/control-plane,cluster.x-k8s.io/cluster-name="$CLUSTER" -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)"
  [[ -n "$CP_IP" ]] && break
  sleep 3
done
# FALLBACK: CAPI can take SEVERAL MINUTES to copy the address into Machine.status
# on a fresh CP. The VM NIC has the private IP immediately, so ask Azure directly
# (mgmt VM has az). RG comes from the AzureCluster so no extra env is needed.
if [[ -z "$CP_IP" ]] && command -v az >/dev/null 2>&1; then
  WL_RG="$(kubectl --context "$CTX" -n "$NAMESPACE" get azurecluster "$CLUSTER" -o jsonpath='{.spec.resourceGroup}' 2>/dev/null || true)"
  if [[ -n "$WL_RG" ]]; then
    echo ">> CP IP not in Machine status yet; querying Azure (rg=$WL_RG)"
    az account show >/dev/null 2>&1 || az login --identity -o none 2>/dev/null || true
    CP_IP="$(az vm list-ip-addresses -g "$WL_RG" --query "[?contains(virtualMachine.name, 'control-plane')].virtualMachine.network.privateIpAddresses" -o tsv 2>/dev/null | head -1 || true)"
  fi
fi
if [[ -n "$APISERVER_HOST" && -n "$CP_IP" ]]; then
  echo ">> pinning kube-proxy apiserver $APISERVER_HOST -> $CP_IP (internal-LB hairpin workaround)"
  kubectl --kubeconfig "$WORKLOAD_KCFG" -n kube-system patch ds kube-proxy --type=merge \
    -p "{\"spec\":{\"template\":{\"spec\":{\"hostAliases\":[{\"ip\":\"$CP_IP\",\"hostnames\":[\"$APISERVER_HOST\"]}]}}}}"
  kubectl --kubeconfig "$WORKLOAD_KCFG" -n kube-system rollout restart ds/kube-proxy
else
  echo ">> WARN: could not resolve apiserver host / CP IP; skipping kube-proxy hostAliases fix"
fi

# --- helm (install to ~/.local/bin if absent; no sudo, fully reversible) --------
# Put ~/.local/bin on PATH up front so get-helm-3's own post-install `helm`
# self-check resolves the freshly dropped binary (otherwise it exits non-zero
# and trips `set -e`, even though the install succeeded).
export PATH="$HOME/.local/bin:$PATH"
if ! command -v helm >/dev/null 2>&1; then
  echo ">> helm not found; installing to ~/.local/bin"
  mkdir -p "$HOME/.local/bin"
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \
    | HELM_INSTALL_DIR="$HOME/.local/bin" USE_SUDO=false bash
fi
echo ">> helm: $(helm version --short)"

# --- 1. cloud-provider-azure (CCM + cloud-node-manager) -------------------------
echo ">> installing cloud-provider-azure (clusterName=$CLUSTER, clusterCIDR=$POD_CIDR)"
helm upgrade --install cloud-provider-azure \
  --repo "$CPA_HELM_REPO" cloud-provider-azure \
  --kubeconfig "$WORKLOAD_KCFG" \
  --namespace kube-system \
  --set infra.clusterName="$CLUSTER" \
  --set cloudControllerManager.clusterCIDR="$POD_CIDR"

# The CCM Deployment shipped by the chart tolerates control-plane/master/etcd but
# NOT `node.cloudprovider.kubernetes.io/uninitialized:NoSchedule` nor
# `node.kubernetes.io/not-ready:NoSchedule` - the exact taints a fresh
# cloud-provider=external node carries. Without these it can never schedule to
# clear the very `uninitialized` taint it owns (deadlock). Add them back.
echo ">> patching CCM tolerations (uninitialized + not-ready:NoSchedule)"
kubectl --kubeconfig "$WORKLOAD_KCFG" -n kube-system patch deploy cloud-controller-manager --type=merge -p '{"spec":{"template":{"spec":{"tolerations":[{"key":"node-role.kubernetes.io/master","effect":"NoSchedule"},{"key":"node-role.kubernetes.io/control-plane","effect":"NoSchedule"},{"key":"node.cloudprovider.kubernetes.io/uninitialized","value":"true","effect":"NoSchedule"},{"key":"node.kubernetes.io/not-ready","operator":"Exists","effect":"NoSchedule"}]}}}}'

# --- 2. Calico CNI (VXLAN — IPIP does NOT work on Azure) ------------------------
# Azure's fabric DROPS IP-in-IP (IP protocol 4) between VMs, so Calico's default
# IPIP encapsulation silently blackholes ALL cross-node pod traffic. Calico must
# run in VXLAN mode (UDP 4789, which Azure allows intra-VNet). Render the vanilla
# manifest for VXLAN:
#   - calico_backend: vxlan          -> no BIRD/BGP
#   - drop -bird-live/-bird-ready     -> BIRD isn't running, so those probes fail
#   - default IPv4 pool IPIP=Never    -> created without IPIP
# (docker.io -> quay.io avoids Docker Hub rate limits, same as the README.)
echo ">> installing Calico $CALICO_VERSION (VXLAN; docker.io -> quay.io)"
curl -sSL "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml" \
  | sed 's|docker.io/calico/|quay.io/calico/|g' \
  | sed 's/calico_backend: "bird"/calico_backend: "vxlan"/' \
  | sed '/- -bird-live/d; /- -bird-ready/d' \
  | sed '/name: CALICO_IPV4POOL_IPIP/{n;s/value: "Always"/value: "Never"/}' \
  | kubectl --kubeconfig "$WORKLOAD_KCFG" apply -f -
# The default IPPool is created by calico-node at runtime, so flip its
# encapsulation to VXLAN once it exists (idempotent; also fixes an IPIP install).
echo ">> ensuring default IPPool uses VXLAN"
for _ in $(seq 1 20); do
  kubectl --kubeconfig "$WORKLOAD_KCFG" get ippool default-ipv4-ippool >/dev/null 2>&1 && break
  sleep 3
done
kubectl --kubeconfig "$WORKLOAD_KCFG" patch ippool default-ipv4-ippool --type=merge \
  -p '{"spec":{"ipipMode":"Never","vxlanMode":"Always"}}' 2>/dev/null || true
kubectl --kubeconfig "$WORKLOAD_KCFG" -n kube-system rollout restart ds/calico-node >/dev/null 2>&1 || true

# --- verify ---------------------------------------------------------------------
echo ">> waiting for registered nodes to go Ready (up to 5m; workers may still be joining)..."
kubectl --kubeconfig "$WORKLOAD_KCFG" wait --for=condition=Ready nodes --all --timeout=300s || true
echo ">> nodes after:"
kubectl --kubeconfig "$WORKLOAD_KCFG" get nodes -o wide
echo ">> kube-system pods:"
kubectl --kubeconfig "$WORKLOAD_KCFG" get pods -n kube-system -o wide
