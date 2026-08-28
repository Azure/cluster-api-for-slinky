# CAPI core CRD SDK

Generated Python Pulumi shim for Cluster API core CRDs from CAPI `v1.13.2`.

The source URL, checksum, generator version, package name, dependency version,
and license are pinned in `pulumi/sdks/sources.json`.

```bash
python3 scripts/regenerate_sdks.py generate --sdk capi-core --check
python3 scripts/regenerate_sdks.py generate --sdk capi-core
```

The post-generation script removes package plugin defaults from the generated
resource and invoke options. The generated resource tokens belong to the
Kubernetes provider; retaining those defaults makes Pulumi try to install a
nonexistent shim provider plugin. Generation occurs in a temporary tree and
only replaces tracked generated files after source and tool validation pass.