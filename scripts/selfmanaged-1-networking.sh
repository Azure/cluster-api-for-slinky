#!/usr/bin/env bash
#
# selfmanaged-1-networking.sh - Phase 0 (non-destructive): pre-create the BYO
# Azure networking that the self-managed workload cluster (`caps-self`) needs
# BEFORE `kubectl apply -f selfmanaged-workload-cluster.yaml`.
#
# WHY THIS SCRIPT EXISTS (the #1 reproducibility gap):
#   The AzureCluster in selfmanaged-workload-cluster.yaml declares an UNMANAGED /
#   BYO VNet (networkSpec.vnet references an existing VNet by name). On a BYO VNet
#   CAPZ v1.23.2 (driving networking via ASO) only *references* the per-subnet
#   auxiliary resources by their CAPZ-derived names — it does NOT create them.
#   If they are missing, the ASO subnet CR fails with
#       400 InvalidResourceReference ... <nsg|natgw|routetable> not found
#   which blocks SubnetsReady -> LoadBalancers -> everything. No CP/worker VM is
#   ever created. Historically these were hand-created with ad-hoc `az` commands
#   (only recorded as comments in selfmanaged-3-addons.sh); this script makes that
#   step reproducible and mentor-portable.
#
# WHAT IT CREATES (idempotent; all Owner-tagged; names match the CAPZ derivation
# so CAPZ adopts them as unmanaged):
#   1. VNet                                   vnet-capz-mi-scus            10.1.0.0/16
#   2. NSGs (empty; default rules + corp NRMS cover the traffic):
#        control-plane subnet  ->             caps-self-controlplane-nsg
#        node subnet           ->             caps-self-node-nsg
#   3. Node route table                       caps-self-node-routetable   (node subnet only)
#   4. Public IPs (Standard, Static)          pip-caps-self-cp-natgw, pip-caps-self-node-natgw-1
#   5. NAT gateways (Standard)                caps-self-cp-natgw, caps-self-node-natgw-1
#        - CP subnet egress is REQUIRED: the CP node has no public IP and the API
#          LB is Internal, so without a NAT gw it cannot pull Calico/CCM images
#          (every addon stuck ImagePullBackOff). CAPZ references caps-self-cp-natgw
#          (declared on the CP subnet in the manifest) but does NOT create it.
#   6. Subnets with the associations above:
#        caps-self-cp-subnet   10.1.1.0/24  nsg=controlplane  natgw=cp-natgw
#        caps-self-node-subnet 10.1.2.0/24  nsg=node          natgw=node-natgw-1  rt=node-routetable
#   7. Global VNet peering BOTH directions between this VNet and the management
#      VNet (vm-capz-mi-devVNET), so the mgmt VM reaches the workload cluster's
#      Internal API-server LB (10.1.1.100) over peering. Both directions must be
#      Connected.
#
# Runs ON the CAPZ management VM (uses the attached UAMI via `az login --identity`).
# CAPZ does NOT manage these resources' lifecycle — delete them by hand (or fold
# into Pulumi later). Everything is parameterized so a second cluster (e.g.
# caps-self2, or a CPU-cheap reproducibility test) can be stood up by overriding
# CLUSTER / RG / *_CIDR / SKU-independent names.
#
# Drive from a workstation with:
#     bash scripts/azure-remote.sh sync
#     bash scripts/azure-remote.sh ssh 'bash scripts/selfmanaged-1-networking.sh'
#
# Env overrides (defaults reproduce the live caps-self SCUS topology):
#   CLUSTER RG LOCATION OWNER
#   VNET VNET_CIDR
#   CP_SUBNET CP_CIDR NODE_SUBNET NODE_CIDR
#   CP_NSG NODE_NSG NODE_RT CP_NATGW NODE_NATGW CP_PIP NODE_PIP
#   PEER (1=create peering, 0=skip) MGMT_RG MGMT_VNET
set -euo pipefail

CLUSTER="${CLUSTER:-caps-self}"
RG="${RG:-rg-capz-mi-scus}"
LOCATION="${LOCATION:-southcentralus}"
OWNER="${OWNER:-t-hernandezc}"

VNET="${VNET:-vnet-capz-mi-scus}"
VNET_CIDR="${VNET_CIDR:-10.1.0.0/16}"

CP_SUBNET="${CP_SUBNET:-${CLUSTER}-cp-subnet}"
CP_CIDR="${CP_CIDR:-10.1.1.0/24}"
NODE_SUBNET="${NODE_SUBNET:-${CLUSTER}-node-subnet}"
NODE_CIDR="${NODE_CIDR:-10.1.2.0/24}"

# CAPZ-derived aux-resource names (control-plane/node NSG + node route table are
# derived from <cluster>+<subnet-role>; the CP NAT gw name is declared on the CP
# subnet in the manifest; the node NAT gw uses CAPZ's default node-egress name).
CP_NSG="${CP_NSG:-${CLUSTER}-controlplane-nsg}"
NODE_NSG="${NODE_NSG:-${CLUSTER}-node-nsg}"
NODE_RT="${NODE_RT:-${CLUSTER}-node-routetable}"
CP_NATGW="${CP_NATGW:-${CLUSTER}-cp-natgw}"
NODE_NATGW="${NODE_NATGW:-${CLUSTER}-node-natgw-1}"
CP_PIP="${CP_PIP:-pip-${CP_NATGW}}"
NODE_PIP="${NODE_PIP:-pip-${NODE_NATGW}}"

# Management VNet to peer with (mgmt VM's VNet — reaches the Internal API LB).
PEER="${PEER:-1}"
MGMT_RG="${MGMT_RG:-rg-capz-mi-dev2}"
MGMT_VNET="${MGMT_VNET:-vm-capz-mi-devVNET}"

TAGS=(Owner="$OWNER")

az account show >/dev/null 2>&1 || az login --identity -o none
echo ">> subscription: $(az account show --query name -o tsv)"
echo ">> cluster=$CLUSTER rg=$RG location=$LOCATION vnet=$VNET ($VNET_CIDR)"

# Small helper: does a resource exist? ($1..$N = az show args producing non-error)
_exists() { "$@" >/dev/null 2>&1; }

# --- 0. resource group ----------------------------------------------------------
if _exists az group show -n "$RG"; then
  echo ">> RG $RG exists"
else
  echo ">> creating RG $RG (Owner tag satisfies the VM-create Azure Policy)"
  az group create -n "$RG" -l "$LOCATION" --tags "${TAGS[@]}" -o none
fi

# --- 1. VNet --------------------------------------------------------------------
if _exists az network vnet show -g "$RG" -n "$VNET"; then
  echo ">> VNet $VNET exists"
else
  echo ">> creating VNet $VNET ($VNET_CIDR)"
  az network vnet create -g "$RG" -n "$VNET" \
    --address-prefixes "$VNET_CIDR" -l "$LOCATION" --tags "${TAGS[@]}" -o none
fi

# --- 2. NSGs (empty; CAPZ references, does not manage rules on a BYO VNet) -------
for nsg in "$CP_NSG" "$NODE_NSG"; do
  if _exists az network nsg show -g "$RG" -n "$nsg"; then
    echo ">> NSG $nsg exists"
  else
    echo ">> creating NSG $nsg"
    az network nsg create -g "$RG" -n "$nsg" -l "$LOCATION" --tags "${TAGS[@]}" -o none
  fi
done

# --- 3. node route table --------------------------------------------------------
if _exists az network route-table show -g "$RG" -n "$NODE_RT"; then
  echo ">> route table $NODE_RT exists"
else
  echo ">> creating route table $NODE_RT (node subnet only)"
  az network route-table create -g "$RG" -n "$NODE_RT" -l "$LOCATION" --tags "${TAGS[@]}" -o none
fi

# --- 4. public IPs + 5. NAT gateways (CP + node egress) -------------------------
# CP egress is a HARD requirement (Internal API LB + no public IP => no egress =>
# addon ImagePullBackOff). Node egress is CAPZ's default for node subnets.
create_natgw() {
  local pip="$1" natgw="$2" role="$3"
  if _exists az network public-ip show -g "$RG" -n "$pip"; then
    echo ">> public IP $pip exists"
  else
    echo ">> creating public IP $pip (Standard, Static) for $role egress"
    az network public-ip create -g "$RG" -n "$pip" \
      --sku Standard --allocation-method Static -l "$LOCATION" --tags "${TAGS[@]}" -o none
  fi
  if _exists az network nat gateway show -g "$RG" -n "$natgw"; then
    echo ">> NAT gateway $natgw exists"
  else
    echo ">> creating NAT gateway $natgw (Standard) -> $pip"
    az network nat gateway create -g "$RG" -n "$natgw" \
      --public-ip-addresses "$pip" -l "$LOCATION" --tags "${TAGS[@]}" -o none
  fi
}
create_natgw "$CP_PIP"   "$CP_NATGW"   "control-plane"
create_natgw "$NODE_PIP" "$NODE_NATGW" "node"

# --- 6. subnets with associations ----------------------------------------------
# CP subnet:   NSG + NAT gw (NO route table — matches the live topology).
# Node subnet: NSG + NAT gw + node route table.
ensure_subnet() {
  local name="$1" cidr="$2" nsg="$3" natgw="$4" rt="${5:-}"
  local -a assoc=(--network-security-group "$nsg" --nat-gateway "$natgw")
  [[ -n "$rt" ]] && assoc+=(--route-table "$rt")
  if _exists az network vnet subnet show -g "$RG" --vnet-name "$VNET" -n "$name"; then
    echo ">> subnet $name exists; ensuring associations"
    az network vnet subnet update -g "$RG" --vnet-name "$VNET" -n "$name" \
      --address-prefixes "$cidr" "${assoc[@]}" -o none
  else
    echo ">> creating subnet $name ($cidr) with associations"
    az network vnet subnet create -g "$RG" --vnet-name "$VNET" -n "$name" \
      --address-prefixes "$cidr" "${assoc[@]}" -o none
  fi
}
ensure_subnet "$CP_SUBNET"   "$CP_CIDR"   "$CP_NSG"   "$CP_NATGW"
ensure_subnet "$NODE_SUBNET" "$NODE_CIDR" "$NODE_NSG" "$NODE_NATGW" "$NODE_RT"

# --- 7. global VNet peering (both directions) -----------------------------------
# The mgmt VM reaches the workload cluster's Internal API-server LB over peering.
# A Standard internal LB IS reachable across GLOBAL peering (Basic is not). Both
# directions must exist and report Connected.
if [[ "$PEER" == "1" ]]; then
  if _exists az network vnet show -g "$MGMT_RG" -n "$MGMT_VNET"; then
    WL_VNET_ID="$(az network vnet show -g "$RG" -n "$VNET" --query id -o tsv)"
    MGMT_VNET_ID="$(az network vnet show -g "$MGMT_RG" -n "$MGMT_VNET" --query id -o tsv)"
    if _exists az network vnet peering show -g "$RG" --vnet-name "$VNET" -n "${CLUSTER}-to-mgmt"; then
      echo ">> peering ${CLUSTER}-to-mgmt exists"
    else
      echo ">> creating peering ${CLUSTER}-to-mgmt (workload -> mgmt)"
      az network vnet peering create -g "$RG" --vnet-name "$VNET" -n "${CLUSTER}-to-mgmt" \
        --remote-vnet "$MGMT_VNET_ID" --allow-vnet-access --allow-forwarded-traffic -o none
    fi
    if _exists az network vnet peering show -g "$MGMT_RG" --vnet-name "$MGMT_VNET" -n "mgmt-to-${CLUSTER}"; then
      echo ">> peering mgmt-to-${CLUSTER} exists"
    else
      echo ">> creating peering mgmt-to-${CLUSTER} (mgmt -> workload)"
      az network vnet peering create -g "$MGMT_RG" --vnet-name "$MGMT_VNET" -n "mgmt-to-${CLUSTER}" \
        --remote-vnet "$WL_VNET_ID" --allow-vnet-access --allow-forwarded-traffic -o none
    fi
  else
    echo ">> WARN: mgmt VNet $MGMT_VNET not found in $MGMT_RG; skipping peering (set PEER=0 to silence)"
  fi
else
  echo ">> PEER=0: skipping VNet peering"
fi

echo
echo ">> networking ready for cluster '$CLUSTER' in $RG:"
echo "   VNet $VNET ($VNET_CIDR)"
echo "   CP   subnet $CP_SUBNET ($CP_CIDR)   nsg=$CP_NSG   natgw=$CP_NATGW"
echo "   node subnet $NODE_SUBNET ($NODE_CIDR) nsg=$NODE_NSG natgw=$NODE_NATGW rt=$NODE_RT"
[[ "$PEER" == "1" ]] && echo "   peered <-> $MGMT_VNET ($MGMT_RG)"
echo ">> next: kubectl apply -f selfmanaged-workload-cluster.yaml"
