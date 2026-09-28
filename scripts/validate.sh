#!/usr/bin/env bash
# Validates Central Dogma manifests against Kubernetes and CRD schemas.
#
# Usage: scripts/validate.sh [overlay-dir...]
#   With no arguments, renders every apps/*/overlays/* and also checks the ArgoCD
#   resources in bootstrap/ and platform/. With arguments, renders only those
#   overlays.
#
# Needs kustomize and kubeconform on PATH.
set -euo pipefail

cd "$(dirname "$0")/.."

# ArgoCD kinds (Application, ApplicationSet) are not in the default schemas.
crd_schemas='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
kubeconform_args=(-strict -summary -schema-location default -schema-location "$crd_schemas")

overlays=("$@")
check_platform=false
if [ ${#overlays[@]} -eq 0 ]; then
  check_platform=true
  shopt -s nullglob
  overlays=(apps/*/overlays/*/)
  shopt -u nullglob
fi

status=0

for dir in "${overlays[@]}"; do
  dir="${dir%/}"
  echo "==> ${dir}"
  if ! kustomize build "$dir" | kubeconform "${kubeconform_args[@]}"; then
    status=1
  fi
done

if [ "$check_platform" = true ]; then
  echo "==> bootstrap/ and platform/"
  if ! kubeconform "${kubeconform_args[@]}" bootstrap/*.yaml platform/*/application.yaml; then
    status=1
  fi
fi

exit "$status"
