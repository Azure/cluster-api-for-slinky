# CAPI core CRD SDK

Generated Python Pulumi shim for Cluster API core CRDs from CAPI `v1.13.2`.

Source CRDs:

```bash
curl -fsSL https://github.com/kubernetes-sigs/cluster-api/releases/download/v1.13.2/core-components.yaml \
  -o pulumi/sdks/capi-core/crds/core-components.yaml
```

Generation:

```bash
crd2pulumi \
  --pythonPath pulumi/sdks/capi-core \
  --pythonName ca4s_capi_core_crds \
  --version 1.13.2 \
  --force \
  pulumi/sdks/capi-core/crds/core-components.yaml
```

After regeneration, keep the local `_utilities.py` provider-default patch so
Pulumi uses the Kubernetes provider rather than trying to install a nonexistent
shim provider plugin.