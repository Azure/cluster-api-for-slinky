"""Helm OCI install of the Pulumi Kubernetes Operator (PKO).

Owns:
  * The ``pulumi-kubernetes-operator`` Namespace.
  * One ``helm.v3.Release`` of
    ``oci://ghcr.io/pulumi/helm-charts/pulumi-kubernetes-operator``
    pinned to :data:`PKO_CHART_VERSION`.

The chart's defaults are already minimal (single-replica operator, no
metrics service, no webhook). We override only the bits we need:
``extraVolumes`` + ``extraVolumeMounts`` + ``extraEnv`` to project the
in-cluster Gitea SSH credentials (private key + ``known_hosts``) into
the operator pod — PKO's stack-controller invokes go-git's
``ListContext`` from inside this pod, so the host-key trust store and
the key bytes must live alongside the controller process.

The SSH Secret is created in the same namespace by
:class:`pko.pko_bootstrap.PKOBootstrap` BEFORE this release reconciles
(a ``depends_on`` edge on the release ties the two together). The pod
must come up with the Secret already in place; absent that, kubelet's
volume mount would block startup, and with ``atomic=True`` the Helm
install would time out and roll back.

Pin policy mirrors the rest of the stack: explicit chart version bumps,
no implicit "latest". Bump :data:`PKO_CHART_VERSION` and review release
notes before letting ``pulumi up`` reconcile it.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import ResourceOptions


# Pinned PKO chart. See https://github.com/pulumi/pulumi-kubernetes-operator
# for the release matrix. v2.x is the current major.
PKO_CHART_OCI = "oci://ghcr.io/pulumi/helm-charts/pulumi-kubernetes-operator"
PKO_CHART_VERSION = "2.3.0"

# The conventional namespace for PKO. We don't make this configurable —
# downstream Stack CRs are pinned to land in the same namespace by the
# component and there is no real use case for renaming it.
PKO_NAMESPACE = "pulumi-kubernetes-operator"

# Volume + mount path for the in-cluster Gitea SSH Secret. Mirrored
# in :mod:`pko._stack_cr` so workspace pods get the same mount layout
# under the same env var — keeps the contract a single constant set.
_SSH_VOLUME_NAME = "gitea-ssh"
_SSH_MOUNT_PATH = "/etc/gitea-ssh"
_SSH_KNOWN_HOSTS_KEY = "known_hosts"


class PKORelease(pulumi.ComponentResource):
    """Namespace + Helm OCI release of PKO.

    Children:
      * ``Namespace/pulumi-kubernetes-operator``
      * ``helm.sh/v3:Release`` of the pinned chart.

    Outputs:
      * ``namespace``  — the namespace name (constant, surfaced as Output
        for DAG-edge tracking).
      * ``release_status`` — the Release's ``status`` Output, anchored so
        downstream Stack CRs wait for the operator to be Ready.
    """

    namespace: pulumi.Output[str]
    release_status: pulumi.Output[object]

    def __init__(
        self,
        name: str,
        *,
        provider: k8s.Provider,
        namespace_resource: pulumi.Resource,
        ssh_secret_name: pulumi.Input[str],
        ssh_secret_resource: pulumi.Resource,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:pko:PKORelease", name, props={}, opts=opts)

        # OCI chart install. ``helm.v3.Release`` treats ``chart`` that
        # starts with ``oci://`` as an OCI ref; no ``repository_opts``
        # needed.
        #
        # The namespace is owned by :class:`pko.pko_bootstrap.PKOBootstrap`
        # (passed in via ``namespace_resource``) rather than this release,
        # so the SSH Secret can be created in the same namespace BEFORE
        # the Helm install kicks off. Without that ordering the chart's
        # Deployment would block on a missing Secret mount and
        # ``atomic=True`` would roll back.
        #
        # ``extraVolumes`` + ``extraVolumeMounts`` + ``extraEnv`` carry
        # the Gitea SSH credentials into the operator pod. ``defaultMode:
        # 0o400`` ships the key bytes with permissions go-git's SSH
        # transport will accept (anything looser than 0o600 is rejected).
        # ``SSH_KNOWN_HOSTS`` overrides go-git's default lookup path so
        # we don't have to write into a user home dir (which may not even
        # exist for the operator pod's uid).
        release = k8s.helm.v3.Release(
            f"{name}-helm",
            chart=PKO_CHART_OCI,
            version=PKO_CHART_VERSION,
            namespace=PKO_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=600,
            values={
                "extraVolumes": [
                    {
                        "name": _SSH_VOLUME_NAME,
                        "secret": {
                            "secretName": ssh_secret_name,
                            "defaultMode": 0o400,
                        },
                    },
                ],
                "extraVolumeMounts": [
                    {
                        "name": _SSH_VOLUME_NAME,
                        "mountPath": _SSH_MOUNT_PATH,
                        "readOnly": True,
                    },
                ],
                "extraEnv": [
                    {
                        "name": "SSH_KNOWN_HOSTS",
                        "value": f"{_SSH_MOUNT_PATH}/{_SSH_KNOWN_HOSTS_KEY}",
                    },
                ],
            },
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                # Both the namespace AND the SSH Secret must exist before
                # the chart's Deployment can come up Ready (atomic=True
                # rolls back otherwise).
                depends_on=[namespace_resource, ssh_secret_resource],
            ),
        )

        self.namespace = pulumi.Output.from_input(PKO_NAMESPACE)
        self.release_status = release.status

        self.register_outputs(
            {
                "namespace": self.namespace,
                "release_status": self.release_status,
            }
        )
