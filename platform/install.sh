#!/usr/bin/env bash
set -euo pipefail
context=${1:?pass the disposable kind context}
[[ "$context" == kind-rag-platform ]] || { echo "expected kind-rag-platform" >&2; exit 2; }
k=(kubectl --context "$context")
"${k[@]}" apply --server-side -f https://github.com/cert-manager/cert-manager/releases/download/v1.19.2/cert-manager.yaml
"${k[@]}" -n cert-manager rollout status deployment/cert-manager-webhook --timeout=150s
"${k[@]}" -n cert-manager rollout status deployment/cert-manager-cainjector --timeout=150s
"${k[@]}" create namespace kserve --dry-run=client -o yaml | "${k[@]}" apply -f -
kserve=https://github.com/kserve/kserve/releases/download/v0.20.0
"${k[@]}" apply --server-side -f "$kserve/kserve-crds.yaml"
"${k[@]}" wait --for=condition=Established crd/clusterstoragecontainers.serving.kserve.io \
  crd/inferenceservices.serving.kserve.io --timeout=120s
"${k[@]}" apply --server-side -f "$kserve/kserve.yaml"
"${k[@]}" -n kserve rollout status deployment/kserve-controller-manager --timeout=180s
"${k[@]}" create namespace argo --dry-run=client -o yaml | "${k[@]}" apply -f -
"${k[@]}" apply --server-side -n argo -f https://github.com/argoproj/argo-workflows/releases/download/v4.1.6/quick-start-minimal.yaml
"${k[@]}" -n argo patch configmap workflow-controller-configmap --type=merge \
  -p '{"data":{"artifactRepository":null}}'
"${k[@]}" -n argo rollout status deployment/workflow-controller --timeout=180s
