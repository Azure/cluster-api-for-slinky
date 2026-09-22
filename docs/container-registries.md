# Container Registry Setup

CA4S uses container registries for two distinct artifact paths:

- Kubernetes nodes pull container images for controllers, webhooks, and
  workloads.
- The Pulumi Kubernetes Operator (PKO) workspace pulls OCI Helm charts while
  deploying workload-cluster components.

Registry reachability depends on the artifact consumer. PKO and CAPI Operator
consume charts and provider bundles from the management cluster, while target
workload-cluster nodes pull container images. Custom source builds and artifact
selection are documented separately in
[Custom CAPI, CAPZ, and Slinky Builds](custom-components.md).

## Local Registries

The local Pulumi stack creates and configures registries through ctlptl. No
manual registry setup is required before `pulumi up`.

Two logical registry routes are used:

- `docker.io` points to a pull-through cache backed by `mirror.gcr.io`. The
  management kind cluster and CAPD workload nodes use it for Docker Hub image
  pulls.
- The writable custom registry stores locally built controller images and OCI
  Helm charts. The outer Pulumi stack builds and pushes the artifacts before
  the inner PKO stack consumes them.

The writable registry is created when a configured build needs a local destination. Its shared
settings live under `ca4s-infra:customRegistry` (`name` and optional `port`), not
inside an individual build. The default name is `custom-registry`; the port is
automatically selected when omitted.

The registry is exposed differently to each consumer:

1. Management-cluster nodes use the ctlptl registry configuration attached to
   the kind cluster.
1. PKO accesses the writable registry through a Service in its namespace. The
   inner Pulumi stack uses that endpoint for plain-HTTP OCI chart pulls.
1. CAPD nodes access host-published registry ports through
   `host.docker.internal`, or through the Docker gateway on Linux.

The outer stack forwards cache and writable-registry coordinates automatically.
Each local workload can add routes or replace a generated route using the
`registryRoutes` map under its workload-cluster configuration:

```yaml
className: local
registryRoutes:
  registry.example:5000:
    server: https://registry.example:5000
    hosts:
      - url: https://mirror.example
        capabilities: [pull]
      - gatewayPort: 5443
        scheme: https
        capabilities: [pull, resolve, push]
  docker.io:
    server: https://registry-1.docker.io
    hosts: []
```

Each entry identifies a containerd registry namespace and describes the
corresponding `hosts.toml` configuration. A host selects exactly one direct
`url` or Docker `gatewayPort`; gateway hosts accept `scheme: http` or `https`.
Capabilities are a nonempty, unique combination of `pull`, `resolve`, and `push`,
defaulting to `[pull, resolve]`. Registry names and URLs are validated, and
bootstrap files are TOML-serialized and shell-quoted. No registry credentials
are accepted in these URLs.

During bootstrap, each CAPD node discovers the Docker gateway once, writes one
`/etc/containerd/certs.d/<registry>/hosts.toml` file per configured route, and
restarts containerd once. An empty `registryRoutes` map leaves the automatic
routes intact; an explicit route with `hosts: []` uses its server directly,
as the Docker Hub override above does. Explicit entries replace the generated
route of the same name, rather than appending duplicate hosts.

The local writable registry is intentionally unauthenticated and uses plain
HTTP. It is suitable only for local development.

### Host Docker Pulls

The ctlptl pull-through cache does not configure the host Docker daemon. Host
Docker directly pulls the initial `kindest/node` image, CAPD DockerMachine node
images, and the `envoyproxy/envoy` image used by cloud-provider-kind.

Configure Docker's own registry mirror separately as described in the
[recommended local setup](../README.md#recommended-local-setup).

### Local Verification

After applying the local stack, inspect the generated registry outputs:

```bash
pushd pulumi
pulumi stack output cache_registry_port -s local
pulumi stack output build_artifact_refs -s local
popd
```

`build_artifact_refs` groups published references by build name, destination
(`local` or `acr`), and repository. Image tags and chart versions are included.
Management-cluster consumers receive separate Service-based URLs through
deployment configuration; those URLs are not standalone publishing outputs.

To verify a CAPD node's containerd routes, inspect a node container returned by
`docker ps`:

```bash
docker exec <capd-node-container> \
  find /etc/containerd/certs.d -name hosts.toml -print -exec cat {} \;
```

Local Slinky charts use Pulumi's Helm v4 `Chart` resource because the Helm v3
`Release` resource does not expose a plain-HTTP OCI option. These resources are
managed and awaited by Pulumi, but they do not appear in `helm list`.

## Azure Container Registry

When any AKS or Azure BYO workloads are configured, the outer stack provisions
one ephemeral Azure Container Registry and its own resource group. Placement
uses the first Azure workload's subscription, location, and tags. The generated
registry name is unique to that resource group. The stack passes its resolved
endpoint and resource ID to both publishers and Azure workload stacks.

The registry uses a fixed configuration: Basic SKU, public HTTPS access, and
standard registry RBAC.

Source-build inputs remain independent of the destination. The two Slurm image
deployment roles automatically publish to ACR for Azure workloads:

```yaml
config:
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

The source revision must be committed. `repositoryUrl` plus `sourceRef` can
also be used instead of a local checkout.

### Artifact Routing

| Artifact and consumer | Registry |
| --- | --- |
| Slurm operator/webhook images for Azure workloads | Ephemeral ACR |
| Slurm operator/webhook images for local workloads | ctlptl |
| CAPZ controller image and provider bundle in Kind | ctlptl |
| Slinky charts pulled by PKO | ctlptl |
| Other named builds | ctlptl |

Mixed local/Azure stacks publish Slurm images to both destinations. Azure-only
image builds do not create an unused local writable registry. Charts and CAPZ
bundles stay local because publishing them to ACR alone would not configure
their management-cluster consumers' authentication. ACR is provisioned even
without custom builds; default image references then remain unchanged.
There is no user-facing registry catalog or existing-ACR selector.

### Publisher Authentication

The ACR provisioning component uses normal Azure Native provider authentication to create
the resource group and ACR. The registry's admin account is disabled.

Publishing uses the outer-stack host's Azure CLI identity. Sign in with
`az login` before running the stack, or use your CI environment's Azure CLI
login flow. That identity must have `AcrPush` or equivalent permissions on the
registry, including inherited permissions. The stack does not create additional
publisher role assignments. Azure Native provider authentication does not
automatically sign the Azure CLI in.

Each publishing or refresh operation runs `az acr login --expose-token` for the
generated registry and its subscription. The short-lived Entra token is passed
to `docker login --password-stdin` with a temporary Docker config directory.
Docker builds/pushes and Helm chart pushes use that directory, which is deleted after the operation;
normal host Docker login stores are not modified. SDK clients use only the
temporary credentials and close their HTTP sessions after each operation.
Authenticated ACR manifest probes use the Azure Container Registry SDK with
Azure CLI credentials so the SDK performs ACR's Entra token exchange. Generic
registry probes and local OCI bundle uploads continue using the ORAS SDK.
Tokens are not passed through Pulumi resource inputs, outputs, or PKO config.
Azure CLI, Docker, and Git must be installed on the outer-stack host; Make and
Helm are required by the corresponding build recipes. ORAS is installed as a
Python dependency, and the ORAS executable is not required.

### Workload-Node Authorization

The outer stack grants `AcrPull` on the generated registry to the identity
used by each workload's nodes. Azure BYO uses the user-assigned identity already
attached to every VM. For AKS, the outer stack creates dedicated control-plane
and kubelet UAMIs in a disposable resource group. It grants the control-plane
UAMI Managed Identity Operator on the kubelet UAMI and Network Contributor
on the configured AKS resource group. CAPZ receives both identity resource IDs
before cluster creation. The kubelet UAMI receives only registry-scoped `AcrPull`.
Both paths let kubelet pull images without image pull secrets or access to the
publisher's identity. The PKO init-stack configuration depends on the completed
outer grants, and teardown keeps them until the inner cluster is deleted.

Azure BYO also installs the cloud-provider-azure `acr-credential-provider` on
every control-plane and worker node before kubeadm runs. The binary is pinned
to version `v1.36.5` and verified against the release SHA-256 for the node's
amd64 or arm64 architecture. Kubelet invokes it only for the generated ACR
hostname. The provider reads `/etc/kubernetes/azure.json`, obtains a token for the
VM's user-assigned identity through Azure Instance Metadata Service, and
returns short-lived ACR credentials to kubelet. No static registry credential
is stored on workload nodes.

New Azure BYO nodes need outbound HTTPS access to GitHub Releases during
bootstrap to download the pinned provider binary. Environments without that
egress must adapt the bootstrap download to a mirror or a preinstalled binary
before enabling ACR-hosted workload images.

The identity running the outer stack must be allowed to create and delete these
ordinary role assignments and create UAMIs. The CAPZ provisioning identity must
be able to assign the UAMIs to AKS. PKO does not create role assignments and
receives no RBAC-administrator delegation. An account whose ABAC condition
excludes administrative roles can still work if it permits `AcrPull`, Managed
Identity Operator, and Network Contributor assignments at the required scopes.
Azure RBAC propagation may cause initial pulls to retry.

### Lifecycle and verification

When used, the ACR and its dedicated resource group belong to the stack that
creates them. Destroying that stack deletes the registry and all its artifacts
according to resource dependencies. This is a development registry, not durable image
storage. No private endpoint, firewall policy, geo-replication, or production
retention policy is configured. Publisher and workload nodes need outbound
HTTPS access to ACR; PKO and CAPI Operator continue using the local registry.
The pull grants, AKS identities, and identity permissions are also outer-stack
owned and removed on teardown after the workload cluster.

Inspect published references:

```bash
pushd pulumi
pulumi up -s <stack>
pulumi stack output build_artifact_refs -s <stack>
popd
```
