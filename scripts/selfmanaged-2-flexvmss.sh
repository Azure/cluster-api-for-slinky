#!/usr/bin/env bash
#
# selfmanaged-2-flexvmss.sh - Phase 2 (non-destructive): pre-create the BYO VMSS Flex
# that the backported CAPZ `virtualMachineScaleSetID` field places workers into, so
# both ND40rs_v2 land in ONE InfiniBand partition (matching pkeys) and cross-node
# RDMA works.
#
# Why this fixes RDMA: the CAPZ-managed Uniform AzureMachinePool created a VMSS with
# singlePlacementGroup=false, so the 2 workers ended up in different IB partitions
# (full-membership pkey 0x8004 vs 0x8005). A Flexible VMSS with platform-fault-domain-
# count=1 backs all its VMs with ONE implicit availability set, which co-locates them
# in a single IB partition. The VMSS is an EMPTY placement container (no VM profile /
# capacity 0); CAPZ adds the worker VMs into it (they bring their own NIC/disk/image).
#
# DO NOT attach a Proximity Placement Group to this Flex VMSS (learned the hard way,
# 2026-06-30): the VMSS PPG propagates to each VM, but the Flex VMSS's implicit
# availability set has no PPG, so Azure rejects VM create with
#   409 OperationNotAllowed: "the VM has a Proximity Placement Group reference
#   whereas the Availability Set does not have a Proximity Placement Group reference".
# FD=1 (single implicit availability set) alone gives the IB co-location.
#
# Runs ON the CAPZ mgmt VM (uses the attached UAMI via `az login --identity`).
# Idempotent. CAPZ does NOT manage this VMSS lifecycle — delete it by hand (or fold
# into Pulumi later).
#
# Env overrides: RG, VMSS, ZONE, OWNER
set -euo pipefail

RG="${RG:-RG-CAPZ-MI-SCUS}"
VMSS="${VMSS:-caps-self-flex}"
ZONE="${ZONE:-1}"
OWNER="${OWNER:-t-hernandezc}"

az account show >/dev/null 2>&1 || az login --identity -o none

# --- empty Flexible VMSS (placement container; FD=1 => one implicit AS => one IB partition) ---
if az vmss show -g "$RG" -n "$VMSS" >/dev/null 2>&1; then
  echo ">> VMSS $VMSS already exists"
else
  echo ">> creating empty Flexible VMSS $VMSS (FD=1, zone $ZONE, NO ppg, 0 instances)"
  az vmss create -g "$RG" -n "$VMSS" \
    --orchestration-mode Flexible --platform-fault-domain-count 1 \
    --zones "$ZONE" --instance-count 0 \
    --tags Owner="$OWNER" -o none
fi

VMSS_ID="$(az vmss show -g "$RG" -n "$VMSS" --query id -o tsv)"
echo
echo ">> VMSS Flex ready. Set this as"
echo "   AzureMachineTemplate.spec.template.spec.virtualMachineScaleSetID:"
echo "   $VMSS_ID"
