#!/bin/bash
kind create cluster --config=kind-config.yaml
kubectl cluster-info --context kind-kind
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
helm repo add awx-operator https://ansible-community.github.io/awx-operator-helm/
helm install my-awx-operator awx-operator/awx-operator -n awx --create-namespace -f awx.yaml
# kubectl get secret awx-admin-password -n awx -o jsonpath="{.data.password}" | base64 --decode ; echo