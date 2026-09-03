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
requires Make and ORAS.

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

### Build and Publish

From a slurm-operator checkout, build and publish both application images:

```bash
docker build --target manager -t registry.example/slurm-operator:feature .
docker build --target webhook -t registry.example/slurm-operator-webhook:feature .
docker push registry.example/slurm-operator:feature
docker push registry.example/slurm-operator-webhook:feature
```

To publish matching charts, use a valid semantic version and run the repository's
version synchronization before packaging:

```bash
make REGISTRY=registry.example VERSION=1.3.0-dev.1 version-match push-charts
```

This publishes `slurm-operator-crds`, `slurm-operator`, and `slurm` beneath
`oci://registry.example/charts`.

### Configure a Workload Cluster

Add the override under the selected workload-cluster entry:

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
helm list -n slinky
helm list -n slurm
```
