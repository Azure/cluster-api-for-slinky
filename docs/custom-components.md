# Custom CAPI, CAPZ, and Slinky Builds

This guide describes how to test custom Cluster API ecosystem components with
CA4S. Customizations are configured in `pulumi/Pulumi.<stack>.yaml`.

## Source Selection

The outer CA4S stack can build images and OCI artifacts from either a local Git
checkout or a remote Git repository. Each source requires exactly one of
`sourcePath` or `repositoryUrl`, plus a `sourceRef` that resolves to a commit.
CA4S builds from a detached worktree at that commit, so uncommitted changes are
not included.

Local source example:

```yaml
sourcePath: /home/user/cluster-api-provider-azure
sourceRef: HEAD
```

Remote source example:

```yaml
repositoryUrl: https://github.com/kubernetes-sigs/cluster-api-provider-azure.git
sourceRef: 69ec3a40a818ccbc32b8ce88c84609404d8cb7a2
```

The automated source-build path uses a ctlptl registry attached to the local
management cluster. It requires Docker and Git. CAPZ artifact generation also
requires Make and ORAS. Pulumi-managed Slinky chart builds require Make and
Helm.

## CAPI

CA4S installs the Cluster API Operator and its core, bootstrap, and control-plane
providers at pinned versions. It does not currently expose source-build or image
overrides for the core CAPI provider or the CAPI Operator itself.

Infrastructure providers installed by CAPI Operator support `providerOci` and
`controllerImage` settings. CA4S automatically supplies these settings for a
custom CAPZ build as described below.

## CAPZ

A complete custom CAPZ deployment has two artifacts:

- The controller image used by the CAPZ manager Deployment.
- The provider OCI artifact containing `metadata.yaml` and
  `infrastructure-components.yaml`, used by CAPI Operator to install the matching
  CRDs, RBAC, and webhooks.

Configure both `customImages.images.capz-controller` and `capzArtifact` from the
same source revision:

```yaml
ca4s-infra:customImages:
  images:
    capz-controller:
      sourcePath: /home/user/cluster-api-provider-azure
      sourceRef: HEAD
      imageName: capz/cluster-api-azure-controller
      buildArgs:
        ARCH: amd64
ca4s-infra:capzArtifact:
  sourcePath: /home/user/cluster-api-provider-azure
  sourceRef: HEAD
  artifactName: capz/cluster-api-provider-azure
```

`repositoryUrl` can replace `sourcePath` for both entries. CA4S builds and pushes
the controller image, runs the CAPZ release-manifest targets, pushes the provider
artifact, and injects their resolved references into the enabled Azure
infrastructure-provider configuration.

Use both overrides when the branch changes APIs, generated manifests, RBAC, or
webhooks. A controller-only change can use only `customImages`, although keeping
the image and provider artifact at the same revision avoids version skew.

## Slinky

Each local, AKS, or Azure BYO workload cluster accepts a `slinky` block that can
override the OCI chart source, each chart version, and the operator and webhook
images. The chart registry must be reachable from the Pulumi Kubernetes
Operator (PKO) runner pod in the management cluster because the inner Pulumi
stack performs the Helm chart pulls there. The image registry must be reachable
from the workload cluster nodes because kubelet pulls the operator and webhook
images there. A single registry used for both purposes must be reachable from
both environments.

### Local Pulumi-Managed Build

The outer Pulumi stack can build the manager image, webhook image, and all three
Slinky charts from one slurm-operator Git revision. Configure two targeted
images and the chart source:

```yaml
ca4s-infra:customImages:
  images:
    slurm-operator:
      sourcePath: /home/user/slurm-operator
      sourceRef: HEAD
      imageName: slurm-operator
      target: manager
    slurm-operator-webhook:
      sourcePath: /home/user/slurm-operator
      sourceRef: HEAD
      imageName: slurm-operator-webhook
      target: webhook
ca4s-infra:slinkyCharts:
  sourcePath: /home/user/slurm-operator
  sourceRef: HEAD
```

`repositoryUrl` can replace `sourcePath`. Use the same `sourceRef` for both
images and the charts. As with CAPZ builds, the source revision must be
committed because builds use detached Git worktrees.

For a portable, committable stack configuration, use the public repository and
pin all artifacts to the same immutable commit SHA:

```yaml
ca4s-infra:customImages:
  images:
    slurm-operator:
      repositoryUrl: https://github.com/SlinkyProject/slurm-operator.git
      sourceRef: ee37d5aaccacca2a2fddb9bd0581e6f8004d4ece
      imageName: slurm-operator
      target: manager
    slurm-operator-webhook:
      repositoryUrl: https://github.com/SlinkyProject/slurm-operator.git
      sourceRef: ee37d5aaccacca2a2fddb9bd0581e6f8004d4ece
      imageName: slurm-operator-webhook
      target: webhook
ca4s-infra:slinkyCharts:
  repositoryUrl: https://github.com/SlinkyProject/slurm-operator.git
  sourceRef: ee37d5aaccacca2a2fddb9bd0581e6f8004d4ece
```

GitHub pull request refs such as `refs/pull/255/head` also work, but they are
mutable. Prefer a full commit SHA when the stack file is intended to reproduce
an exact build.

When `slinkyCharts` is configured, both well-known custom images are required.
CA4S also validates that `slurm-operator` uses the `manager` target and
`slurm-operator-webhook` uses the `webhook` target, preventing a chart from
referencing an image that was built from the wrong final stage.

Pulumi performs the remaining wiring automatically for local workload clusters:

1. The manager and webhook Dockerfile targets are built and pushed to the
   ctlptl custom registry.
1. The three charts are assigned a deterministic version derived from the Git
   commit, packaged, and pushed to the same registry.
1. The registry is exposed through a Service in the PKO namespace. The inner
   Pulumi stack pulls charts through that Service using plain-HTTP OCI.
1. CAPD nodes receive one containerd `hosts.toml` entry per configured local
  registry. Pulls are redirected to each registry's host-published port through
  `host.docker.internal`, or through the Docker gateway on Linux.
1. The resolved image references, chart source, and chart version are injected
   into each local workload cluster's Slinky configuration.

The outer stack forwards registry routes to each local workload cluster as one
`registries` list. Each entry identifies the containerd registry namespace and
carries a typed representation of its `hosts.toml` configuration:

```yaml
registries:
  - registry: docker.io
    config:
      server: https://registry-1.docker.io
      hosts:
        - gatewayPort: 5002
  - registry: custom-registry:5000
    config:
      server: http://custom-registry:5000
      hosts:
        - gatewayPort: 5003
          capabilities: [pull, resolve]
```

The outer stack decides what each route means: the `docker.io` entry points at
the pull-through cache, while the named custom entry points at the writable
artifact registry. The workload stack only renders the supplied containerd
configuration. Each host supports `http` or `https` and any non-empty,
non-duplicated combination of `pull`, `resolve`, and `push` capabilities;
defaults are `http` and `[pull, resolve]`.

Bootstrap discovers the Docker gateway once, writes all registry host
configurations, and restarts containerd once. An empty `registries` list writes
no containerd registry override.

The local ctlptl registry intentionally uses unauthenticated plain HTTP and is
for development environments only.

No `slinky` block is required under the local workload cluster when using this
automated path. Explicit values there remain useful for external registries or
partial overrides.

The local plain-HTTP path uses Pulumi's Helm v4 `Chart` resource because the
Helm v3 `Release` resource does not expose a plain-HTTP OCI option. Resources
are still awaited and dependency ordered, but they are managed directly by
Pulumi rather than recorded as native Helm releases. Consequently, `helm list`
does not show these three local chart deployments.

Every workload cluster also installs `slurm-bridge` chart `1.2.2`. CA4S creates
a `Token` backed by the Slurm chart's `slurm-auth-jwt` key, uses the generated
`slurm-bridge-token` Secret, and targets the `compute` Slurm partition. Bridge
admission, controller, and scheduler pods run on controller nodes. Slurm
NodeSet pods tolerate the bridge's `slinky.slurm.net/managed-node` `NoExecute`
taint so the bridge can co-schedule Kubernetes workloads on mapped Slurm nodes
without evicting `slurmd`.

MCS isolation is not configured yet. Bridge workloads must remain exclusive
until Slurm MCS configuration is added.

After `pulumi up`, inspect the generated references:

```bash
pulumi stack output custom_image_refs -s <stack>
pulumi stack output slinky_chart_oci_prefix -s <stack>
pulumi stack output slinky_chart_version -s <stack>
```

### External Registry

AKS and Azure BYO nodes cannot access the developer machine's ctlptl registry.
Build and publish images and charts to a registry reachable from both PKO and
the workload nodes, then add an explicit `slinky` block under the selected
workload-cluster entry:

```bash
docker build --target manager -t registry.example/slurm-operator:feature .
docker build --target webhook -t registry.example/slurm-operator-webhook:feature .
docker push registry.example/slurm-operator:feature
docker push registry.example/slurm-operator-webhook:feature
make REGISTRY=registry.example VERSION=1.3.0-dev.1 version-match push-charts
```

```yaml
ca4s-infra:initStack:
  tenants:
    workloadClusters:
      local:
        className: local
        slinky:
          chartOciPrefix: oci://registry.example/charts
          operatorCrdsChartVersion: 1.3.0-dev.1
          operatorChartVersion: 1.3.0-dev.1
          slurmChartVersion: 1.3.0-dev.1
          operatorImage:
            repository: registry.example/slurm-operator
            tag: feature
          webhookImage:
            repository: registry.example/slurm-operator-webhook
            tag: feature
          imagePullSecrets: []
```

Images require exactly one of `tag` or `digest`. For example, a digest-pinned
image uses:

```yaml
operatorImage:
  repository: registry.example/slurm-operator
  digest: sha256:0123456789abcdef
```

`imagePullSecrets` contains names of pre-created pull secrets in the `slinky`
namespace.

The three chart versions are independent. A code-only manager or webhook change
can retain the published CRD and Slurm chart versions. When a change modifies
APIs or generated CRDs, publish and select the matching
`slurm-operator-crds` chart. Select the matching `slurm` chart when its rendered
custom resources or values schema changed.

## Apply and Verify

Apply the selected stack after publishing all referenced artifacts:

```bash
pushd pulumi
pulumi up -s <stack> --yes
popd
```

Verify the installed images and releases against the workload cluster:

```bash
kubectl -n slinky get deployment slurm-operator \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl -n slinky get deployment slurm-operator-webhook \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
# For external-registry deployments backed by Helm v3 Release:
helm list -n slinky
helm list -n slurm
```
