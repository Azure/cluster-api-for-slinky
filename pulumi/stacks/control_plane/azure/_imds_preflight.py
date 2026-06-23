"""Host-side IMDS preflight for the ``azure`` stack.

What this module does
---------------------
Before the resource graph for the ``azure`` stack is built, we hit the
Azure Instance Metadata Service (IMDS) from the host running ``pulumi
up`` and confirm two things:

1. **IMDS is reachable.** A failed connection here almost always means
   the stack is being run off-Azure, or on an Azure VM whose network
   namespace can't see the link-local IMDS address.
2. **The UAMI is actually attached to this VM.** IMDS at
   ``/metadata/instance/compute/identity`` lists the user-assigned
   identities attached to the VM by their ARM resource ID. We don't
   know the ARM resource ID up front (only the ``clientId`` GUID the
   operator typed into config), so we use ``/metadata/identity/oauth2/token``
   instead: if IMDS can mint a token for that ``client_id``, the UAMI
   is attached. If not, it returns an error like::

       {\"error\":\"invalid_request\",
        \"error_description\":\"Identity not found\"}

   ...which is exactly the failure mode we want to catch at plan time,
   not eight minutes into a CAPZ reconcile loop.

Why host-side and not in-cluster
--------------------------------
Phase 1 deliberately doesn't ship a cluster-side preflight Job (the
user wants docs-only for in-cluster routing). The host-side check is
the closest equivalent: kind nodes typically share the host VM's IMDS
view via the docker bridge, so if the host can hit IMDS, the kind
nodes (and therefore the CAPZ pod once it's reconciling) almost always
can too. The IMDS routing comment block in
:mod:`stacks.control_plane.azure._cluster_identity` walks the network
path.

Skipping the preflight
----------------------
Set the Pulumi config key ``skip_imds_preflight`` to ``true`` on the
azure stack to skip both checks. Useful for:

* CI that runs ``pulumi preview`` off-Azure.
* Unit tests that exercise :func:`stack_azure.run` in isolation.
* Deliberate off-Azure dev loops once the user has set the UAMI
  identifiers manually.

If skipped, the resource graph is built unchanged \u2014 the
AzureClusterIdentity CR will still apply against the management
cluster, and the failure mode just moves back to where it was before
this preflight existed (a CAPZ reconcile error against ARM).
"""

from __future__ import annotations

from typing import Final

import requests


IMDS_TOKEN_URL: Final = (
    "http://169.254.169.254/metadata/identity/oauth2/token"
    "?api-version=2018-02-01&resource=https%3A%2F%2Fmanagement.azure.com%2F"
)
IMDS_HEADERS: Final = {"Metadata": "true"}
# Short enough that a non-Azure host fails fast; long enough that a
# briefly-busy IMDS endpoint on Azure doesn't false-positive.
IMDS_TIMEOUT_SECONDS: Final = 5.0


class ImdsPreflightError(RuntimeError):
    """Raised when IMDS preflight cannot confirm the UAMI is attached."""


def check_uami_attached(
    client_id: str,
    *,
    session: requests.Session | None = None,
) -> None:
    """Confirm IMDS will mint a token for the UAMI ``client_id``.

    Args:
        client_id: UAMI ``clientId`` GUID. Pulumi config already
            validated this is a GUID; we treat it as opaque here.
        session: Optional injection point for tests. When ``None`` we
            use the module-level :mod:`requests` API.

    Raises:
        ImdsPreflightError: if IMDS is unreachable, returns an HTTP
            error, returns a JSON ``error`` body, or returns a 200 that
            doesn't actually contain an ``access_token``.
    """
    get = session.get if session is not None else requests.get
    try:
        response = get(
            f"{IMDS_TOKEN_URL}&client_id={client_id}",
            headers=IMDS_HEADERS,
            timeout=IMDS_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        raise ImdsPreflightError(
            "IMDS at 169.254.169.254 is not reachable from this host: "
            f"{exc!s}. Either run pulumi up from an Azure VM that has "
            f"UAMI client_id={client_id!r} attached, or set "
            "``skip_imds_preflight=true`` on the azure stack."
        ) from exc

    # IMDS uses 400/401 for \"identity not attached\" but the JSON body
    # is the authoritative signal \u2014 surface it verbatim.
    try:
        payload = response.json()
    except ValueError as exc:
        raise ImdsPreflightError(
            f"IMDS returned non-JSON status={response.status_code} "
            f"body={response.text!r}"
        ) from exc

    if isinstance(payload, dict) and "error" in payload:
        raise ImdsPreflightError(
            f"IMDS refused to mint a token for client_id={client_id!r}: "
            f"{payload.get('error')!r} \u2014 "
            f"{payload.get('error_description', '<no description>')!r}. "
            "Most common cause: the UAMI with that client_id is not "
            "attached to this VM. Confirm with: "
            "`az vm identity show --resource-group <rg> --name <vm>`."
        )

    if response.status_code != 200 or not (
        isinstance(payload, dict) and payload.get("access_token")
    ):
        raise ImdsPreflightError(
            f"IMDS returned an unexpected response status="
            f"{response.status_code} body={payload!r}"
        )
