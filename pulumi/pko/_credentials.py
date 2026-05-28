"""Live cross-namespace projection of the GitOps credentials Secret.

PKO's ``Stack.spec.gitAuth.basicAuth.{userName,password}`` are
``SecretKeySelector``-shaped: they reference a Secret by ``name`` and
``key``, but the Secret MUST live in the same namespace as the Stack
CR. Our Stack CRs all live in ``pulumi-kubernetes-operator``; our
``GitOpsRepository`` impls (``GiteaBuiltinRepository`` today) put the
credentials Secret in the gitea namespace.

This module bridges the two via the External Secrets Operator (ESO):
an ``ExternalSecret`` in the PKO namespace whose store is a
``SecretStore`` of provider type ``kubernetes`` pointing at the
upstream Secret. ESO reconciles continuously, so out-of-band
upstream rotation (a credential manager patching the source Secret,
chart-driven password rotation, future Vault-backed credentials)
propagates to the projection within ``refreshInterval`` — no
``pulumi up`` required.

Scope
-----
This component owns ONLY the ``SecretStore`` + ``ExternalSecret``
CRs in the PKO namespace. The RBAC scaffolding that lets ESO
actually read the upstream Secret — a reader SA in the source ns,
plus the TokenRequest grant to the ESO controller SA — is owned
separately by :class:`pko._eso_source_access.ESOSourceAccess`.
Callers wire the two together by passing the grant's
``reader_service_account_name`` / ``reader_service_account_namespace``
Outputs in here, and declaring this component ``depends_on`` the
grant.

Lifetime
--------
The ``ExternalSecret`` CR owns the projected Secret via
``target.creationPolicy: Owner``. When this component is torn down,
the ExternalSecret goes away and ESO garbage-collects the
projection. The upstream Secret is untouched (read-only from this
side).
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import ResourceOptions

# Name of the projected Secret in PKO's namespace. Stack CR
# ``gitAuth.basicAuth`` references point at this name.
PROJECTED_SECRET_NAME = "gitea-credentials"

# How often ESO re-reads the upstream Secret. 60s balances
# "rotation propagates promptly" against controller workload. Tune
# per env if needed.
_REFRESH_INTERVAL = "1m"

# ESO API version this module targets. ``v1beta1`` is stable across
# the 0.9.x — 0.10.x chart line pinned in
# :data:`pko._eso_release.ESO_CHART_VERSION`; bump in lockstep
# when/if we move to 0.11+ and adopt ``v1``.
_ESO_API_VERSION = "external-secrets.io/v1beta1"


class CredentialsProjection(pulumi.ComponentResource):
    """Sync an upstream credentials Secret into the PKO namespace via ESO.

    Owns the ``SecretStore`` + ``ExternalSecret`` CRs in PKO's
    namespace; the reader SA + RBAC plumbing in the source ns is
    owned by :class:`pko._eso_source_access.ESOSourceAccess` and
    passed in via the ``reader_service_account_*`` inputs.

    Inputs:
        upstream_secret_name:             Source Secret name.
        upstream_secret_namespace:        Source Secret namespace.
        pko_namespace:                    Destination namespace.
        reader_service_account_name:      Name of the SA ESO
                                          impersonates in the source
                                          ns (output of the matching
                                          ``ESOSourceAccess``).
        reader_service_account_namespace: Namespace of that SA (same
                                          source).
        provider:                         Kubernetes provider.

    Outputs:
        projected_secret_name:      Constant ``gitea-credentials``,
                                    threaded through the ExternalSecret
                                    CR so consumers' Pulumi DAG edges
                                    correctly wait for the CR to land
                                    (PKO retries Stack reconciles if the
                                    destination Secret isn't materialized
                                    by ESO yet).
        projected_secret_namespace: Echo of ``pko_namespace``, same
                                    DAG-edge treatment.
    """

    projected_secret_name: pulumi.Output[str]
    projected_secret_namespace: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        upstream_secret_name: pulumi.Input[str],
        upstream_secret_namespace: pulumi.Input[str],
        pko_namespace: pulumi.Input[str],
        reader_service_account_name: pulumi.Input[str],
        reader_service_account_namespace: pulumi.Input[str],
        provider: k8s.Provider,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:pko:CredentialsProjection", name, props={}, opts=opts
        )

        # ---- SecretStore (PKO ns) ------------------------------------
        # Namespace-scoped (not ClusterSecretStore) so only
        # ExternalSecrets in PKO ns can use this store. Provider:
        # kubernetes, pointing at the same cluster's API server,
        # impersonating the reader SA owned by ESOSourceAccess.
        store = k8s.apiextensions.CustomResource(
            f"{name}-store",
            api_version=_ESO_API_VERSION,
            kind="SecretStore",
            metadata={
                "name": f"{PROJECTED_SECRET_NAME}-store",
                "namespace": pko_namespace,
            },
            spec={
                "provider": {
                    "kubernetes": {
                        "remoteNamespace": upstream_secret_namespace,
                        "server": {
                            "url": "https://kubernetes.default.svc",
                            # kube-root-ca.crt is auto-projected into
                            # every namespace by kube-apiserver; use
                            # the one in PKO ns so ESO doesn't need
                            # cross-ns ConfigMap read RBAC.
                            "caProvider": {
                                "type": "ConfigMap",
                                "name": "kube-root-ca.crt",
                                "namespace": pko_namespace,
                                "key": "ca.crt",
                            },
                        },
                        "auth": {
                            "serviceAccount": {
                                "name": reader_service_account_name,
                                "namespace": (
                                    reader_service_account_namespace
                                ),
                            },
                        },
                    },
                },
            },
            opts=ResourceOptions(parent=self, provider=provider),
        )

        # ---- ExternalSecret (PKO ns) ---------------------------------
        # Owns the destination Secret via creationPolicy=Owner: when
        # this CR is deleted, ESO garbage-collects the projection.
        # ESO reconciles every ``_REFRESH_INTERVAL``; out-of-band
        # rotations propagate within that window.
        external_secret = k8s.apiextensions.CustomResource(
            f"{name}-external-secret",
            api_version=_ESO_API_VERSION,
            kind="ExternalSecret",
            metadata={
                "name": PROJECTED_SECRET_NAME,
                "namespace": pko_namespace,
            },
            spec={
                "refreshInterval": _REFRESH_INTERVAL,
                "secretStoreRef": {
                    "name": store.metadata.name,
                    "kind": "SecretStore",
                },
                "target": {
                    "name": PROJECTED_SECRET_NAME,
                    "creationPolicy": "Owner",
                },
                "data": [
                    {
                        "secretKey": "username",
                        "remoteRef": {
                            "key": upstream_secret_name,
                            "property": "username",
                        },
                    },
                    {
                        "secretKey": "password",
                        "remoteRef": {
                            "key": upstream_secret_name,
                            "property": "password",
                        },
                    },
                ],
            },
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[store],
            ),
        )

        # Outputs thread the dependency through the ExternalSecret CR so
        # downstream consumers (Stack CRs) wait for the CR to land
        # before referencing the projected Secret name. The destination
        # Secret itself is created by ESO out-of-band; PKO Stack CRs
        # retry on missing Secret, so a few extra reconcile cycles
        # bridge the small gap between CR-apply and ESO-materialize.
        self.projected_secret_name = external_secret.metadata.apply(
            lambda _: PROJECTED_SECRET_NAME
        )
        self.projected_secret_namespace = external_secret.metadata.apply(
            lambda _: pko_namespace
        )

        self.register_outputs(
            {
                "projected_secret_name": self.projected_secret_name,
                "projected_secret_namespace": self.projected_secret_namespace,
            }
        )

