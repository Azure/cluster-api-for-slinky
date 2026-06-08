"""``file://`` state backend for inner Pulumi stacks run by PKO.

PKO's workspace pods need a Pulumi state backend to store the state of
each inner stack (init, control-plane, workload-cluster). Options
include Pulumi Cloud, S3 / GCS / Azure Blob, or a self-managed
``file://`` URL.

For the local dev loop we choose ``file://`` on a PVC mounted into
every workspace pod, so that:

* No external account / credentials are required to bring up the stack.
* State persists across pod restarts and operator restarts.
* The same passphrase Secret unlocks all inner stacks' state.

Owns
----
* One ``PersistentVolumeClaim`` (default storage class — ``local-path``
  on kind) sized at 1Gi.
* One ``RandomPassword`` for the file:// backend's
  ``PULUMI_CONFIG_PASSPHRASE``.
* One ``Secret`` holding that passphrase in PKO's namespace, ready to
  be referenced by every inner Stack CR's
  ``spec.envRefs.PULUMI_CONFIG_PASSPHRASE``.

The PVC is mounted at ``/state`` in every workspace pod and the inner
Stack CRs all use ``spec.backend: file:///state``. See
:func:`pko._stack_cr.build_stack_spec` for the patch that wires it in.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
import pulumi_random as random
from pulumi import ResourceOptions

# Convention: the PVC and Secret live in the PKO namespace alongside the
# operator pod. The names are NOT configurable — every Stack CR's
# ``workspaceTemplate`` patch references these exact names.
STATE_PVC_NAME = "pko-state"
STATE_MOUNT_PATH = "/state"
STATE_BACKEND_URL = f"file://{STATE_MOUNT_PATH}"

# Single Secret name shared across all inner Stack CRs.
PASSPHRASE_SECRET_NAME = "pko-state-passphrase"
PASSPHRASE_SECRET_KEY = "PULUMI_CONFIG_PASSPHRASE"


class StateBackend(pulumi.ComponentResource):
    """PVC + passphrase Secret for the inner stacks' file:// backend.

    Inputs:
        namespace: Pre-existing PKO namespace name (see :class:`pko._release.PKORelease`).
        provider:  Kubernetes provider scoped to the management cluster.

    Outputs:
        pvc_name:                  Name of the PVC (constant).
        passphrase_secret_name:    Name of the passphrase Secret (constant).
        backend_url:               file:// URL inner stacks point at (constant).
    """

    pvc_name: pulumi.Output[str]
    passphrase_secret_name: pulumi.Output[str]
    backend_url: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        namespace: pulumi.Input[str],
        provider: k8s.Provider,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:pko:StateBackend", name, props={}, opts=opts)

        # PVC sized conservatively. State files are small (KB-MB per
        # stack); 1Gi covers years of growth across a handful of stacks.
        # No explicit storageClassName: we accept the cluster's default,
        # which is ``local-path`` on kind.
        pvc = k8s.core.v1.PersistentVolumeClaim(
            f"{name}-pvc",
            metadata={"name": STATE_PVC_NAME, "namespace": namespace},
            spec={
                "access_modes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "1Gi"}},
            },
            opts=ResourceOptions(parent=self, provider=provider),
        )

        # 64 chars of entropy. Stored in Pulumi state (encrypted via the
        # outer stack's existing passphrase salt) and base64-encoded in
        # a k8s Secret for each workspace pod to read at start time.
        passphrase = random.RandomPassword(
            f"{name}-passphrase",
            length=64,
            special=False,
            opts=ResourceOptions(parent=self),
        )

        secret = k8s.core.v1.Secret(
            f"{name}-passphrase-secret",
            metadata={"name": PASSPHRASE_SECRET_NAME, "namespace": namespace},
            string_data={PASSPHRASE_SECRET_KEY: passphrase.result},
            type="Opaque",
            immutable=True,
            opts=ResourceOptions(parent=self, provider=provider),
        )

        self.pvc_name = pvc.metadata.name
        self.passphrase_secret_name = secret.metadata.name
        self.backend_url = pulumi.Output.from_input(STATE_BACKEND_URL)

        self.register_outputs(
            {
                "pvc_name": self.pvc_name,
                "passphrase_secret_name": self.passphrase_secret_name,
                "backend_url": self.backend_url,
            }
        )
