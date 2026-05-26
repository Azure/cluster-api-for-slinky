#!/bin/bash

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

# =============================================================================
# DEPRECATED — DO NOT USE
# =============================================================================
# This script is kept only as a historical reference for the manual command
# sequence. It is NOT exercised in CI and is known to be out of date with the
# rest of the repo. Concretely:
#
#   * The kind cluster + image registry are now provisioned by the Pulumi
#     stack at pulumi/ (see README "Bringing up the management
#     cluster"). The sed-based render below targets a stale on-disk
#     ctlptl.yaml whose port handling no longer matches what downstream
#     consumers expect.
#   * The registry's host port is allocated at runtime by Pulumi and exposed
#     via `pulumi -C pulumi stack output registry_port -s local`; the
#     hardcoded `host.docker.internal:5000` references below are wrong.
#   * Cluster context is no longer `kind-kind` — it is the Pulumi-managed
#     `kind-mgmt-*` context (`pulumi stack output context -s local`).
#
# Use the Pulumi bootstrap instead. This file will be removed in a future
# cleanup pass.
# =============================================================================

# Create a local Kubernetes cluster (and its local registry) as the management cluster
sed "s|\${HOME}|$HOME|g" ctlptl.yaml | ctlptl apply -f -
kubectl cluster-info --context kind-kind

# TODO: remove this workaround by allowing AWX to use manual projects directly
helm repo add gitea-charts https://dl.gitea.com/charts/
helm install gitea gitea-charts/gitea

# install AWX operator
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
helm repo add awx-operator https://ansible-community.github.io/awx-operator-helm/
helm install my-awx-operator awx-operator/awx-operator -n awx --create-namespace -f awx.yaml
# display your AWX admin password
kubectl get secret awx-admin-password -n awx -o jsonpath="{.data.password}" | base64 --decode ; echo

# install CAPD and create cluster
export CLUSTER_TOPOLOGY=true
clusterctl init --infrastructure docker
kubectl apply -f capi-quickstart.yaml
clusterctl get kubeconfig capi-quickstart > capi-quickstart.kubeconfig
# https://cluster-api.sigs.k8s.io/clusterctl/developers#fix-kubeconfig-when-using-docker-desktop-and-clusterctl
# Point the kubeconfig to the exposed port of the load balancer, rather than the inaccessible container IP.
sed -i -e "s/server:.*/server: https:\/\/$(docker port capi-quickstart-lb 6443/tcp | sed "s/0.0.0.0/127.0.0.1/")/g" ./capi-quickstart.kubeconfig
# CAPD nodes won't be ready until we install CNI
kubectl --kubeconfig=./capi-quickstart.kubeconfig apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.29.3/manifests/calico.yaml