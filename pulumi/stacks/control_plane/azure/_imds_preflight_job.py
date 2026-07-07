"""In-cluster IMDS preflight Job for the ``azure`` stack.

What this module does
---------------------
Submit a one-shot ``batch/v1`` Job into ``capz-system`` that asks the
Azure Instance Metadata Service (IMDS) at ``169.254.169.254`` for a
``management.azure.com`` token bound to the configured UAMI's
``client_id``. If IMDS mints a token, the Job exits 0 (Pulumi sees
``condition=Complete=True`` and unblocks). If IMDS returns an error or
the network path is broken, the Job exits non-zero and Pulumi surfaces
the failure as a stack error.

The local-kind stack performs host-side IMDS discovery in plain Python before
declaring resources. This in-cluster preflight runs *after* CAPI Operator +
CAPZ have rolled out, in the same namespace CAPZ itself runs in, and catches
drift the host-side discovery cannot see:

    * a CNI swap (e.g. kindnet -> Cilium with strict default-deny)
      that drops link-local egress from pods,
    * a proxy/firewall change on the host that blocks pod -> IMDS
      without blocking host -> IMDS,
    * a Pod Security Admission tightening that prevents the CAPZ
      controller from acquiring the netns it needs.

The Job runs in ``capz-system`` (not ``default``) so it traverses the
same Docker bridge SNAT + host-VM IMDS route the CAPZ controller pod
will traverse at reconcile time. Same blast radius, same routing.

Image + securityContext
-----------------------
The Job pod uses ``curlimages/curl:8.10.1`` (a 3MB Alpine image with
``curl`` and ``sh``). Pod security:

* ``runAsNonRoot: true``, ``runAsUser: 100``, ``runAsGroup: 101``. The
  ``curlimages/curl`` image's default user is named ``curl_user`` (UID
  100) -- setting a **numeric** uid here makes Pod Security
  Admission's "restricted" profile happy. Without an explicit numeric
  uid, PSA rejects the pod with ``container has runAsNonRoot and image
  has non-numeric user (curl_user), cannot verify user is non-root``
  (this is the same gotcha that bit our manual ``kubectl debug``
  attempt against the CAPZ pod).
* ``seccompProfile: RuntimeDefault`` at pod level.
* Container drops ALL capabilities, disables privilege escalation,
  uses ``readOnlyRootFilesystem`` (curl + sh need nothing writable on
  /). The shell script writes nothing to disk.

Why no retries
--------------
``restartPolicy: Never`` + ``backoffLimit: 0``. We deliberately do not
want the Job to retry on failure:

* Host-side discovery already smoke-tested the clientId, so a failure
    here is a real signal (CNI drift / proxy / etc.), not flake.
* A retry loop would slow ``pulumi up`` down on legitimately broken
  setups and mask the first error in retry noise. Fail fast, surface
  the IMDS response body verbatim, let the operator fix and re-run.

Gating with ``pulumi.com/waitFor=condition=Complete``
-----------------------------------------------------
The Job carries ``pulumi.com/waitFor=condition=Complete`` so
pulumi-kubernetes blocks on the Job's ``Complete`` condition (the
batch/v1 Job sets this to True on the first successful pod
completion). Downstream gating on the Job's ``metadata.name`` Output
therefore implicitly waits for IMDS to verify-out before any consumer
treats the control plane as ready.

Skipping
--------
``IMDSPreflightJob(..., skip=True)`` returns immediately without creating the
Job. This is intended only for targeted tests or one-off debugging; the normal
Azure stack path runs the in-cluster probe.
"""

from __future__ import annotations

from dataclasses import dataclass

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from lib.outputs import CompositeOutput
from stacks.kubernetes_annotations import pulumi_wait_for


# Image pinned by tag (not digest) to match what the host-side runbook
# in /memories/session/phase-a-result.md uses. Tag is immutable on
# Docker Hub for curlimages/curl per their release policy; if we ever
# want belt-and-suspenders, swap to a sha256: digest. ~3MB on disk.
IMDS_PROBE_IMAGE = "curlimages/curl:8.10.1"

# UID/GID of the ``curl_user`` account baked into curlimages/curl.
# Explicit numeric values are required so the Pod Security Admission
# "restricted" profile can verify the pod is non-root without consulting
# the image metadata (which exposes the user as the non-numeric string
# ``curl_user`` and is therefore rejected by PSA).
_CURL_UID = 100
_CURL_GID = 101

# ``capz-system`` is created by the CAPI Operator's azure
# InfrastructureProvider CR. Hardcoded here (rather than imported from
# capi/_operator) to avoid a circular dependency between the azure
# package and the capi package; the value is upstream-defined and
# stable across CAPZ releases.
_CAPZ_SYSTEM_NAMESPACE = "capz-system"

# Pulumi-kubernetes blocks on this condition's transition to True.
# ``Complete`` is the batch/v1 Job condition kubernetes flips on
# successful completion (vs. ``Failed`` on terminal failure). With
# ``backoffLimit: 0`` + ``restartPolicy: Never`` there is no retry, so
# the first pod's exit code is decisive.
_WAIT_FOR_COMPLETE = "condition=Complete"

# IMDS endpoint + parameters. Same shape the host-side preflight uses;
# api-version pinned to 2018-02-01 (the GA version for the identity
# endpoint, stable since well before CAPZ v1.0).
_IMDS_TOKEN_URL = (
    "http://169.254.169.254/metadata/identity/oauth2/token"
    "?api-version=2018-02-01"
    "&resource=https%3A%2F%2Fmanagement.azure.com%2F"
)
_IMDS_CURL_TIMEOUT_SECONDS = 5
_CAPZ_WEBHOOK_READY_URL = (
    "https://capz-webhook-service.capz-system.svc"
    "/validate-infrastructure-cluster-x-k8s-io-v1beta1-azureclusteridentity"
)

# Job-level safety net. Kubernetes counts image pull and scheduling
# time against activeDeadlineSeconds, so this must allow cold pulls of
# curlimages/curl on a fresh kind worker. The actual IMDS network probe
# still fails fast via _IMDS_CURL_TIMEOUT_SECONDS once the container
# starts.
_JOB_ACTIVE_DEADLINE_SECONDS = 10 * 60
_JOB_CREATE_TIMEOUT = "11m"

# Shell script the probe container runs. Single shell command so we can
# fit it on one ``-c`` argv element. The grep -q at the end is the
# decisive bit: a successful IMDS response includes ``access_token``;
# any other response (HTTP 4xx, JSON error body, empty body, timeout)
# fails the grep and the Job exits non-zero -- Pulumi then surfaces
# the curl output in the Stack diagnostics.
def _probe_command(client_id: str) -> list[str]:
    url = f"{_IMDS_TOKEN_URL}&client_id={client_id}"
    script = (
        "set -e\n"
        'echo "[preflight] probing CAPZ webhook service"\n'
        f"webhook_status=$(curl -sS -k -m {_IMDS_CURL_TIMEOUT_SECONDS} "
        "-o /dev/null -w '%{http_code}' "
        f"'{_CAPZ_WEBHOOK_READY_URL}')\n"
        'echo "[preflight] CAPZ webhook HTTP status: $webhook_status"\n'
        'echo "[preflight] curling IMDS for client_id ' + client_id + '"\n'
        f"response=$(curl -sS -m {_IMDS_CURL_TIMEOUT_SECONDS} "
        "-H 'Metadata: true' --noproxy '*' "
        f"'{url}')\n"
        'echo "[preflight] IMDS response: $response"\n'
        # Decisive check. ``grep -q access_token`` is intentionally
        # naive: a real access_token field always implies a successful
        # mint, and on failure the response is JSON {error,
        # error_description, ...} which won't contain that string.
        'echo "$response" | grep -q access_token\n'
        'echo "[preflight] PASS: IMDS minted a token for the UAMI"\n'
    )
    return ["sh", "-c", script]


@dataclass(frozen=True)
class IMDSPreflightJobOutputs(CompositeOutput):
    job_name: Output[str]
    job_namespace: Output[str]


class IMDSPreflightJob(pulumi.ComponentResource):
    """In-cluster IMDS preflight Job in ``capz-system``.

    Args:
        name:
            Pulumi resource name. Used as a prefix for the Job's
            ``metadata.name``.
        client_id:
            UAMI ``clientId`` GUID. The Job asks IMDS for a token bound
            to this identity; success implies (a) IMDS is reachable
            from inside ``capz-system``, and (b) the UAMI is attached
            to the host VM.
        namespace:
            Namespace to submit the Job into. Defaults to
            ``capz-system`` so the test pod traverses the same Docker
            bridge / host-route path the CAPZ controller will use.
            Override only for testing.
        skip:
            When True, the component creates no Job and exposes an
            no output object. Use only when the
            host-side preflight has also been skipped.
        provider:
            Kubernetes provider for the management cluster. None means
            the ambient provider (suitable when running inside a PKO
            workspace pod against the in-cluster API).
        opts:
            Standard ``pulumi.ResourceOptions``. Typically
            ``ResourceOptions(parent=..., depends_on=[capi_release])``
            so the Job waits for the CAPZ Deployment + the
            ``capz-system`` namespace to exist.

    Outputs:
        outputs:
            ``IMDSPreflightJobOutputs`` when the Job exists, otherwise
            ``None`` when ``skip=True``.
    """

    outputs: IMDSPreflightJobOutputs | None

    def __init__(
        self,
        name: str,
        *,
        client_id: pulumi.Input[str],
        namespace: str = _CAPZ_SYSTEM_NAMESPACE,
        skip: bool = False,
        provider: k8s.Provider | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:azure:IMDSPreflightJob", name, props={}, opts=opts
        )

        if skip:
            self.outputs = None
            self.register_outputs({"job": None})
            return

        # Use Output.from_input so the Job spec can interpolate the
        # client_id even when it arrives as an Output (typical when
        # the caller passes ``spec.client_id`` straight through).
        # The shell command is rendered server-side by Pulumi after
        # the Output resolves.
        client_id_output = Output.from_input(client_id)
        command = client_id_output.apply(_probe_command)

        job = k8s.batch.v1.Job(
            f"{name}-job",
            metadata={
                "name": "imds-preflight",
                "namespace": namespace,
                "annotations": pulumi_wait_for(_WAIT_FOR_COMPLETE),
                "labels": {
                    "app.kubernetes.io/name": "imds-preflight",
                    "app.kubernetes.io/component": "control-plane-azure",
                    "app.kubernetes.io/managed-by": "pulumi",
                },
            },
            spec={
                # No retries (see module docstring "Why no retries").
                "backoffLimit": 0,
                # Catches a hung pod / image pull / unreachable IMDS
                # that would otherwise block pulumi up indefinitely.
                "activeDeadlineSeconds": _JOB_ACTIVE_DEADLINE_SECONDS,
                # Keep the failed pod around for kubectl logs after a
                # bad apply. Successful pods are garbage-collected by
                # the Job TTL controller eventually.
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {
                        "labels": {
                            "app.kubernetes.io/name": "imds-preflight",
                            "app.kubernetes.io/component": (
                                "control-plane-azure"
                            ),
                        },
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        # Pod-level securityContext. See module
                        # docstring "Image + securityContext" for why
                        # the numeric uid/gid matter.
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": _CURL_UID,
                            "runAsGroup": _CURL_GID,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "probe",
                                "image": IMDS_PROBE_IMAGE,
                                "command": command,
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                    "readOnlyRootFilesystem": True,
                                },
                                # No resource limits: this is a
                                # short-lived shell + one HTTP call.
                                # Requesting tiny values to be a good
                                # cluster citizen if scheduling is
                                # tight.
                                "resources": {
                                    "requests": {
                                        "cpu": "10m",
                                        "memory": "16Mi",
                                    },
                                    "limits": {
                                        "cpu": "100m",
                                        "memory": "64Mi",
                                    },
                                },
                            }
                        ],
                    },
                },
            },
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                # custom_timeouts give pulumi-kubernetes its own
                # ceiling on the waitFor poll loop. Slightly larger
                # than activeDeadlineSeconds so a Job that hits its
                # own deadline reports as Failed (clean error) rather
                # than as a Pulumi create-timeout (less clean).
                custom_timeouts=pulumi.CustomTimeouts(
                    create=_JOB_CREATE_TIMEOUT
                ),
            ),
        )

        # Echo metadata.name as an Output so downstream gates have
        # something to depend on.
        job_name = job.metadata["name"].apply(  # type: ignore[union-attr]
            lambda n: n
        )
        self.outputs = IMDSPreflightJobOutputs(
            job_name=job_name,
            job_namespace=Output.from_input(namespace),
        )

        self.register_outputs(self.outputs.to_outputs())
