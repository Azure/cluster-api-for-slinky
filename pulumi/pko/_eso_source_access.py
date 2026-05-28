"""Per-source-namespace RBAC grant for ESO impersonation.

ESO's ``kubernetes`` provider authenticates to a source namespace by
minting bound tokens for a ServiceAccount in that namespace via the
TokenRequest API. This component owns the scaffolding for one such
grant — i.e., it gives ESO the ability to read one specific upstream
Secret in one specific source namespace, and nothing else.

What it creates (all in the source namespace):

* A reader ``ServiceAccount`` (the identity ESO impersonates).
* ``Role`` + ``RoleBinding`` granting that SA ``get``/``list``/``watch``
  on the named upstream Secret, scoped via ``resourceNames``.
* ``Role`` + ``RoleBinding`` granting the ESO controller SA ``create``
  on ``serviceaccounts/token`` and ``get`` on ``serviceaccounts``,
  both scoped via ``resourceNames`` to the reader SA.

What it does NOT create:

* The ESO install itself (see :mod:`pko._eso_release`).
* The ``SecretStore`` / ``ExternalSecret`` CRs that consume the grant
  (see :mod:`pko._credentials`).

One instance per ``(source_namespace, upstream_secret)`` tuple. The
consumer (``CredentialsProjection``) ``depends_on`` the component as
a whole so its ``SecretStore`` CR doesn't ship before the RBAC is
applied.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions


class ESOSourceAccess(pulumi.ComponentResource):
    """RBAC grant for ESO to read one specific upstream Secret.

    Inputs:
        upstream_secret_name:                 Source Secret name. Also
                                              drives the derived reader
                                              SA / Role / RoleBinding
                                              names so multiple grants
                                              in the same source ns
                                              don't collide.
        upstream_secret_namespace:            Source Secret namespace.
        eso_namespace:                        Namespace ESO is installed
                                              into; subject namespace of
                                              the TokenRequest
                                              RoleBinding.
        eso_controller_service_account_name:  SA the ESO controller runs
                                              as; subject of the
                                              TokenRequest RoleBinding.
        provider:                             Kubernetes provider.

    Outputs:
        reader_service_account_name:      Name of the SA ESO
                                          impersonates; consumed by
                                          ``CredentialsProjection``'s
                                          ``SecretStore.spec.provider.
                                          kubernetes.auth.serviceAccount.name``.
                                          Threaded through the
                                          underlying ``ServiceAccount``
                                          resource so consumer DAG edges
                                          wait for SA creation.
        reader_service_account_namespace: Echo of
                                          ``upstream_secret_namespace``;
                                          consumed at the same site for
                                          the SA's ``namespace`` field.
    """

    reader_service_account_name: pulumi.Output[str]
    reader_service_account_namespace: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        upstream_secret_name: pulumi.Input[str],
        upstream_secret_namespace: pulumi.Input[str],
        eso_namespace: pulumi.Input[str],
        eso_controller_service_account_name: pulumi.Input[str],
        provider: k8s.Provider,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:pko:ESOSourceAccess", name, props={}, opts=opts
        )

        # Kubernetes resource names derived from the upstream Secret
        # name so multiple grants in the same source ns don't collide.
        # These are Outputs (upstream_secret_name is Input[str]) — that's
        # fine for ``metadata.name``; the Pulumi resource names (first
        # positional arg) stay deterministic plain strings.
        reader_sa_name = Output.concat(upstream_secret_name, "-eso-reader")
        token_role_name = Output.concat(
            upstream_secret_name, "-eso-token-issuer"
        )

        # ---- Reader SA + read-grant ----------------------------------
        # The SA ESO impersonates. Lives in the source namespace because
        # the kubernetes-provider auth flow issues a TokenRequest against
        # an SA in that namespace.
        reader_sa = k8s.core.v1.ServiceAccount(
            f"{name}-reader-sa",
            metadata={
                "name": reader_sa_name,
                "namespace": upstream_secret_namespace,
            },
            opts=ResourceOptions(parent=self, provider=provider),
        )

        # Tightest possible read scope: a single Secret by name. Not the
        # whole Secret collection of the source namespace.
        reader_role = k8s.rbac.v1.Role(
            f"{name}-reader-role",
            metadata={
                "name": reader_sa_name,
                "namespace": upstream_secret_namespace,
            },
            rules=[
                {
                    "api_groups": [""],
                    "resources": ["secrets"],
                    "resource_names": [upstream_secret_name],
                    "verbs": ["get", "list", "watch"],
                },
            ],
            opts=ResourceOptions(parent=self, provider=provider),
        )

        reader_rb = k8s.rbac.v1.RoleBinding(
            f"{name}-reader-rb",
            metadata={
                "name": reader_sa_name,
                "namespace": upstream_secret_namespace,
            },
            subjects=[
                {
                    "kind": "ServiceAccount",
                    "name": reader_sa.metadata.name,
                    "namespace": upstream_secret_namespace,
                },
            ],
            role_ref={
                "api_group": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": reader_role.metadata.name,
            },
            opts=ResourceOptions(parent=self, provider=provider),
        )

        # ---- TokenRequest RBAC for the ESO controller SA -------------
        # ESO mints short-lived bound tokens for the reader SA via the
        # TokenRequest API. Granted in the source ns (where the reader
        # SA lives), scoped to that one SA, bound to the ESO controller
        # SA in its own ns. ESO also needs ``get`` on the SA itself to
        # look up its metadata before issuing the request.
        token_role = k8s.rbac.v1.Role(
            f"{name}-token-role",
            metadata={
                "name": token_role_name,
                "namespace": upstream_secret_namespace,
            },
            rules=[
                {
                    "api_groups": [""],
                    "resources": ["serviceaccounts/token"],
                    "resource_names": [reader_sa.metadata.name],
                    "verbs": ["create"],
                },
                {
                    "api_groups": [""],
                    "resources": ["serviceaccounts"],
                    "resource_names": [reader_sa.metadata.name],
                    "verbs": ["get"],
                },
            ],
            opts=ResourceOptions(parent=self, provider=provider),
        )

        k8s.rbac.v1.RoleBinding(
            f"{name}-token-rb",
            metadata={
                "name": token_role_name,
                "namespace": upstream_secret_namespace,
            },
            subjects=[
                {
                    "kind": "ServiceAccount",
                    "name": eso_controller_service_account_name,
                    "namespace": eso_namespace,
                },
            ],
            role_ref={
                "api_group": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": token_role.metadata.name,
            },
            opts=ResourceOptions(parent=self, provider=provider),
        )

        # Outputs surface the reader SA identity to the SecretStore in
        # :mod:`pko._credentials`. Threading through ``reader_sa.metadata``
        # ties consumer DAG edges to SA creation; component-level
        # ``depends_on`` of this grant covers the rest of the RBAC.
        self.reader_service_account_name = reader_sa.metadata.name
        self.reader_service_account_namespace = reader_rb.metadata.namespace

        self.register_outputs(
            {
                "reader_service_account_name": (
                    self.reader_service_account_name
                ),
                "reader_service_account_namespace": (
                    self.reader_service_account_namespace
                ),
            }
        )
