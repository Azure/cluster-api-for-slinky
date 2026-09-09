# Container Registry Setup

CA4S uses container registries for two distinct artifact paths:

- Kubernetes nodes pull container images for controllers, webhooks, and
  workloads.
- The Pulumi Kubernetes Operator (PKO) workspace pulls OCI Helm charts while
  deploying workload-cluster components.

A registry used for custom Slinky artifacts must therefore be reachable from
both the PKO runner in the management cluster and the target workload-cluster
nodes. Custom source builds and artifact selection are documented separately in
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

The registry is exposed differently to each consumer:

1. Management-cluster nodes use the ctlptl registry configuration attached to
   the kind cluster.
1. PKO accesses the writable registry through a Service in its namespace. The
   inner Pulumi stack uses that endpoint for plain-HTTP OCI chart pulls.
1. CAPD nodes access host-published registry ports through
   `host.docker.internal`, or through the Docker gateway on Linux.

The outer stack forwards these routes to each local workload cluster as a typed
`registries` list:

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

Each entry identifies a containerd registry namespace and describes the
corresponding `hosts.toml` configuration. Each host supports `http` or `https`
and any non-empty, non-duplicated combination of `pull`, `resolve`, and `push`
capabilities. Defaults are `http` and `[pull, resolve]`.

During bootstrap, each CAPD node discovers the Docker gateway once, writes one
`/etc/containerd/certs.d/<registry>/hosts.toml` file per configured route, and
restarts containerd once. An empty `registries` list does not change
containerd's registry configuration.

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
pulumi stack output custom_image_refs -s local
pulumi stack output slinky_chart_oci_prefix -s local
pulumi stack output slinky_chart_version -s local
popd
```

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

> **TODO:** Add first-class Azure Container Registry (ACR) provisioning and
> configuration.

The ACR design must cover both registry consumers rather than only workload
node image pulls:

- Provision or select an ACR and publish custom images and OCI Helm charts.
- Authenticate the PKO workspace for OCI chart pulls without embedding
  long-lived credentials in stack configuration.
- Grant AKS and Azure BYO workload nodes permission to pull images.
- Define the identity and role-assignment model for local management clusters,
  AKS management clusters, and workload clusters.
- Represent the ACR endpoint and authentication settings in the workload
  cluster configuration without weakening the typed local registry model.
- Add deployment tests that verify chart pulls from PKO and image pulls from
  every supported workload-cluster class.

Until that work is complete, publish artifacts to an external registry that is
reachable from both PKO and the workload nodes, then configure explicit Slinky
image references and an OCI chart prefix as described in the
[external registry workflow](custom-components.md#external-registry).
