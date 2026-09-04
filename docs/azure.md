# Azure Deployment Notes

CA4S supports managed AKS clusters and Azure BYO clusters powered by Cluster API
Provider Azure (CAPZ). This document covers configuration specific to Azure
deployments.

## Reuse The Host Resource Group

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