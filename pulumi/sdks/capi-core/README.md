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

python3 scripts/patch_crd_sdk_provider_defaults.py
```

The post-generation script removes package plugin defaults from the generated
resource and invoke options. The generated resource tokens belong to the
Kubernetes provider; retaining those defaults makes Pulumi try to install a
nonexistent shim provider plugin. Run the script with `--check` to verify that
committed SDK output contains the canonical patch.