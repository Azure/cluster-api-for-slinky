# Generated Pulumi SDKs

The importable Python SDK sources in this directory are generated and tracked.
Disposable package output such as `build/`, `dist/`, `*.egg-info/`,
`__pycache__/`, and bytecode is not tracked.

`sources.json` pins each upstream source, version, license, generator, and CRD
checksum. Install the pinned tools before regenerating:

```bash
go install github.com/pulumi/crd2pulumi@v1.6.2
pulumi version
```

Use the repository-owned workflow from the repository root:

```bash
python3 scripts/regenerate_sdks.py verify
python3 scripts/regenerate_sdks.py generate --all --check
python3 scripts/regenerate_sdks.py generate --sdk awx
python3 scripts/regenerate_sdks.py clean --check
python3 scripts/regenerate_sdks.py clean
```

Generation takes place in a temporary directory. CRD source checksums and the
`crd2pulumi` Go module version are validated before output is compared or
copied. Hand-maintained README, license, attributes, ignore, and pinned CRD
input files are preserved.