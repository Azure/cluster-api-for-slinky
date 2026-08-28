# Flux source-controller CRD SDK

Generated Python Pulumi shim for the Flux source-controller `GitRepository`
CRD. The source CRD is maintained in
[`fluxcd/source-controller`](https://github.com/fluxcd/source-controller) under
the Apache License 2.0.

The exact source-controller tag used for the currently committed output was not
retained. Before regenerating this SDK, select and record a source-controller
tag, save its CRD manifest as a generation input, and update this document with
the source URL and version.

After running `crd2pulumi` from the repository root, apply the deterministic
provider-default patch:

```bash
python3 scripts/patch_crd_sdk_provider_defaults.py
```

The post-generation script removes package plugin defaults from the generated
resource and invoke options. The generated resource tokens belong to the
Kubernetes provider; retaining those defaults makes Pulumi try to install a
nonexistent shim provider plugin. Run the script with `--check` to verify that
committed SDK output contains the canonical patch.