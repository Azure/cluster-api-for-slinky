# Custom CAPI, CAPZ, and Slinky Builds

This guide describes how to test custom Cluster API ecosystem components with
CA4S. Customizations are configured in `pulumi/Pulumi.<stack>.yaml`.

## Build Configuration

`ca4s-infra:builds` is a map of named builds. Every entry selects a `builder`
and a Git source. The outer stack validates builder-specific options, then
injects planning and build functions into the generic publisher.

| Builder | Options beyond the Git source | Outputs |
| --- | --- | --- |
| `docker-image` | Required `imageName`; optional `target` and string-valued `buildArgs` | One image |
| `capz-bundle` | None | CAPZ provider bundle |
| `slinky-charts` | None | Three Slinky charts |

Registry provisioning is independent of these builds. Optional shared settings:

```yaml
ca4s-infra:customRegistry:
  name: custom-registry
  port: 5003
```

The name defaults to `custom-registry`; omitting the port lets ctlptl select one.
The local writable registry is created only for builds using that destination.
Local publications use `build-<name>` resources; ACR publications use
`build-<name>-acr`. Each may publish multiple artifacts.
`build_artifact_refs` is a map from build name to destination (`local` or `acr`)
to repository/reference map, so a mixed stack can expose both copies.
It is the single publishing output for images, bundles, and charts. Each
reference includes its tag and uses the configured registry consumer endpoint.
Management-cluster Service URLs for PKO and CAPI Operator are derived separately
in deployment wiring and are not exported as publishing references.

Build names also select the following deployment roles:

| Build name | Required builder | Consumer |
| --- | --- | --- |
| `capz-controller` | `docker-image` | Enabled CAPZ controller |
| `capz-provider` | `capz-bundle` | Enabled Azure infrastructure provider in CAPI Operator |
| `slurm-operator` | `docker-image` | Workload Slurm operator |
| `slurm-operator-webhook` | `docker-image` | Workload Slurm webhook |
| `slinky-charts` | `slinky-charts` | Workload Slinky chart deployments via PKO |

Other names publish artifacts without automatic deployment overrides. The
`slinky-charts` deployment role requires both Slurm image builds. These rules
belong to stack consumer wiring, not the generic build configuration. Image
targets can be any stage in the selected source Dockerfile, or omitted.

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

The automated source-build path uses ctlptl for management-cluster artifacts
and local images, and ephemeral ACR for Azure workload Slurm images. It requires Docker and Git. CAPZ artifact generation also
requires Make; OCI bundle publishing uses the ORAS Python SDK from the Python
requirements, not the ORAS executable. Pulumi-managed Slinky chart builds require Make and
Helm. Registry topology and runtime configuration are documented in
[Container Registry Setup](container-registries.md).

### Publisher Composition

The internal `artifacts` package separates Git source handling, build recipes,
and registry access. One `PublishedArtifacts` resource owns the lifecycle and
accepts two injected functions: `plan(options, commit)` returns repository/tag
pairs, and `build(worktree, options, tags, session)` builds and publishes them.
The publisher has no recipe-name dispatch or fixed list of supported builds.
The stack selects image, CAPZ, or Slinky functions and supplies the appropriate
ctlptl or ACR destination. The same functions serve both destination types.
Destination selection is stack code, not a
user-facing registry catalog.

```python
from artifacts import PublishedArtifacts, RegistryDestination
from artifacts import capz

destination: RegistryDestination = {
  "server": "localhost:5002",
  "consumer_server": "custom-registry:5000",
  "plain_http": True,
}
bundle = PublishedArtifacts(
  "capz-artifact",
  plan=capz.artifact_tags,
  build=capz.build_and_publish,
  source_path="/src/cluster-api-provider-azure",
  source_ref="HEAD",
  destination=destination,
)
```

The image recipe accepts `build_options` containing `image_name`, optional
`target`, and optional `build_args`. CAPZ and Slinky use their fixed build
recipes. Outputs are maps keyed by repository: `artifact_refs`,
`host_artifact_refs`, and `artifact_tags`, plus `source_commit` and `built`.
For example, Slinky produces three entries while CAPZ produces one bundle.

The shared lifecycle resolves Git, checks the planned outputs, and builds
inside a temporary worktree only when an artifact is missing. Build and publish
run in the same operation; a failure still cleans up the worktree. Recipe
functions do not implement Pulumi create, update, refresh, or diff handling.

The functions are serialized as part of the dynamic provider implementation,
not passed as resource inputs. Source, destination, and build options remain
inputs; changes to the serialized provider also trigger an update check. The
optional `resource_id_repository` preserves a grouping identity for builds
such as Slinky's chart set, without teaching the publisher about that recipe.

For ACR, both endpoints use its login server, `plain_http` is false, and
`acr_resource_id` selects the registry and subscription for Entra login. The
registry session owns authentication, transport flags, and manifest checks.
The ORAS Python SDK handles reference parsing, authenticated registry requests,
and OCI bundle uploads. Docker still builds and pushes images; Helm packages
and pushes charts so Helm's manifest format remains owned by Helm. Temporary
build paths and authentication tokens do not become Pulumi outputs.

The SDK has no manifest-existence API, so the session uses its authenticated
request method for a HEAD probe and treats only HTTP 404 as absent. Each SDK
operation gets an isolated client with only the temporary registry credentials;
the adapter seeds the SDK credential cache because its default loader also
merges host credentials. No registry token-exchange or blob-upload protocol is
implemented by CA4S.

Registry provisioning and consumer access remain separate. In particular,
publishing Helm charts to ACR does not grant PKO permission to pull them; node
`AcrPull` grants apply only to node image pulls. The stack still constructs
management-cluster Service URLs for local artifact consumers.

Use `PublishedArtifacts` from `artifacts` for publishing. ORAS constructs the
CAPZ bundle manifest, while Docker and Helm retain their native formats rather
than being wrapped as generic file bundles. No parallel OCI manifest model is
introduced.

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

Configure both `builds.capz-controller` and `builds.capz-provider` from the
same source revision:

```yaml
ca4s-infra:builds:
  capz-controller:
    builder: docker-image
    sourcePath: /home/user/cluster-api-provider-azure
    sourceRef: HEAD
    imageName: capz/cluster-api-azure-controller
    buildArgs:
      ARCH: amd64
  capz-provider:
    builder: capz-bundle
    sourcePath: /home/user/cluster-api-provider-azure
    sourceRef: HEAD
```

`repositoryUrl` can replace `sourcePath` for both entries. CA4S builds and pushes
the controller image, runs the CAPZ release-manifest targets, pushes the provider
artifact to the fixed `capz/cluster-api-provider-azure` repository, and injects
their resolved references into the enabled Azure infrastructure-provider
configuration.

The outer stack publishes the CAPZ controller, provider bundle, and Slinky
charts to the ctlptl registry in Kind. Slurm images use ACR for Azure workloads
and ctlptl for local workloads.
See [Artifact Routing](container-registries.md#artifact-routing).

Use both overrides when the branch changes APIs, generated manifests, RBAC, or
webhooks. A controller-only change can use only `builds.capz-controller`, although keeping
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

### Pulumi-Managed Build

The outer Pulumi stack can build the manager image, webhook image, and all three
Slinky charts from one slurm-operator Git revision. Configure two targeted
images and the chart source:

```yaml
ca4s-infra:builds:
  slurm-operator:
    builder: docker-image
    sourcePath: /home/user/slurm-operator
    sourceRef: HEAD
    imageName: slurm-operator
    target: manager
  slurm-operator-webhook:
    builder: docker-image
    sourcePath: /home/user/slurm-operator
    sourceRef: HEAD
    imageName: slurm-operator-webhook
    target: webhook
  slinky-charts:
    builder: slinky-charts
    sourcePath: /home/user/slurm-operator
    sourceRef: HEAD
```

`repositoryUrl` can replace `sourcePath`. Use the same `sourceRef` for both
images and the charts. As with CAPZ builds, the source revision must be
committed because builds use detached Git worktrees.

For a portable, committable stack configuration, use the public repository and
pin all artifacts to the same immutable commit SHA:

```yaml
ca4s-infra:builds:
  slurm-operator:
    builder: docker-image
    repositoryUrl: https://github.com/SlinkyProject/slurm-operator.git
    sourceRef: c284b9577df89472bf3b91c04ae582d1545da5c7
    imageName: slurm-operator
    target: manager
  slurm-operator-webhook:
    builder: docker-image
    repositoryUrl: https://github.com/SlinkyProject/slurm-operator.git
    sourceRef: c284b9577df89472bf3b91c04ae582d1545da5c7
    imageName: slurm-operator-webhook
    target: webhook
  slinky-charts:
    builder: slinky-charts
    repositoryUrl: https://github.com/SlinkyProject/slurm-operator.git
    sourceRef: c284b9577df89472bf3b91c04ae582d1545da5c7
```

GitHub pull request refs such as `refs/pull/255/head` also work, but they are
mutable. Prefer a full commit SHA when the stack file is intended to reproduce
an exact build.

When `builds.slinky-charts` is configured, both well-known image builds are required.
The image keys select deployment overrides, not Dockerfile stages. Each image's
optional `target` selects a stage from its source Dockerfile; omitting it builds
the final stage. The examples use the upstream `manager` and `webhook` stages.

Pulumi performs the remaining wiring automatically:

1. The configured operator and webhook images are built and pushed to the
  registry for their workload environment: ctlptl locally, ACR on Azure, or both.
1. The three charts are assigned a deterministic version derived from the Git
  commit, packaged, and pushed to ctlptl.
1. The registry is exposed through a Service in the PKO namespace. The inner
   Pulumi stack pulls charts through that Service using plain-HTTP OCI.
1. CAPD nodes receive one containerd `hosts.toml` entry per configured local
   registry.
1. The resolved image references, chart source, and chart version are injected
  into each workload cluster's Slinky configuration. Azure workloads receive
  the ACR coordinates and grant their node identities pull access.

See [Local Registries](container-registries.md#local-registries) for the
pull-through cache, writable artifact registry, typed `registryRoutes`
configuration, CAPD containerd routing, and verification commands.

No `slinky` block is required under workload clusters when using this automated
path. Explicit values remain useful for partial overrides.

The local plain-HTTP path uses Pulumi's Helm v4 `Chart` resource because the
Helm v3 `Release` resource does not expose a plain-HTTP OCI option. Resources
are still awaited and dependency ordered, but they are managed directly by
Pulumi rather than recorded as native Helm releases. Consequently, `helm list`
does not show these three local chart deployments. See
[Container Registry Setup](container-registries.md) for details about which
clients use each local registry route.

Every workload cluster also installs `slurm-bridge` chart `1.2.2`. CA4S creates
a `Token` backed by the Slurm chart's `slurm-auth-jwt` key, uses the generated
`slurm-bridge-token` Secret, and targets the `compute` Slurm partition. Bridge
admission, controller, and scheduler pods run on controller nodes. Slurm
NodeSet pods tolerate the bridge's `slinky.slurm.net/managed-node` `NoExecute`
taint so the bridge can co-schedule Kubernetes workloads on mapped Slurm nodes
without evicting `slurmd`.

MCS isolation is not configured yet. Bridge workloads must remain exclusive
until Slurm MCS configuration is added.

Local and Azure BYO workload clusters use Kubernetes 1.36 or newer and enable
the `GenericWorkload` and `WorkloadWithJob` feature gates together with the
`scheduling.k8s.io/v1alpha2` API. This enables native `Workload` and `PodGroup`
gang scheduling, including the Job controller's `spec.scheduling` integration.
AKS does not expose these managed control-plane flags. CA4S does not yet install
the JobSet controller; JobSet workloads require that addon separately.

After `pulumi up`, inspect the generated references:

```bash
pulumi stack output build_artifact_refs -s <stack>
```

For example, the `slinky-charts.local` entry contains references for
`charts/slurm-operator-crds`, `charts/slurm-operator`, and `charts/slurm`, each
with its generated chart version as the tag.

### Azure Workloads

The outer stack provisions one ephemeral ACR when AKS or Azure BYO workloads
are configured. Slurm operator and webhook builds publish there automatically;
mixed stacks also publish local copies. CAPZ bundles and Slinky charts remain
in ctlptl for management-cluster consumers. The same build recipes and Entra
registry session are reused, with no extra registry catalog configuration.

ACR publishing uses the host's signed-in Azure CLI identity with `AcrPush` or
equivalent permissions. The registry session obtains short-lived Entra tokens
automatically; no manual `az acr login` or registry credential Secret is required.
The ACR component disables the admin account. Workload image pulls can use node
managed identities; chart and provider-bundle pulls need consumer authentication.

For Azure-published images, inspect the build's `acr` entry:

```bash
pulumi stack output build_artifact_refs -s <stack>
```

Destroying the stack that owns the ACR component deletes the registry and all its artifacts. It is
intended for ephemeral development environments, not durable image storage.
See [Azure Container Registry](container-registries.md#azure-container-registry)
for placement, authentication, permissions, and network requirements.

## Apply and Verify

Apply the selected stack to provision registries, publish custom builds, and
deploy the workload components:

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
