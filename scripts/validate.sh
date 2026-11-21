#!/usr/bin/env bash
# Validates Central Dogma manifests against Kubernetes and CRD schemas.
#
# Usage: scripts/validate.sh [overlay-dir...]
#   With no arguments, renders every apps/*/overlays/* and every Kustomize
#   directory in platform/, and also checks the ArgoCD resources in bootstrap/
#   and platform/ and the Kafka resources in streams/. With arguments, renders
#   only those overlays.
#
# Needs kustomize and kubeconform on PATH.
set -euo pipefail

cd "$(dirname "$0")/.."

# ArgoCD kinds (Application, ApplicationSet) are not in the default schemas.
crd_schemas='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
# Neither catalog has schemas for ApicurioRegistry3 or CustomResourceDefinition
# (vendor CRDs come pinned from upstream releases).
kubeconform_args=(-strict -summary -schema-location default -schema-location "$crd_schemas" -skip ApicurioRegistry3,CustomResourceDefinition)

overlays=("$@")
check_platform=false
if [ ${#overlays[@]} -eq 0 ]; then
  check_platform=true
  shopt -s nullglob
  overlays=(apps/*/overlays/*/)
  shopt -u nullglob
  # Platform components rendered with Kustomize (bases and overlays).
  while IFS= read -r kustomization; do
    overlays+=("$(dirname "$kustomization")")
  done < <(find platform -name kustomization.yaml | sort)
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
  mapfile -t applications < <(find platform -name application.yaml | sort)
  if ! kubeconform "${kubeconform_args[@]}" bootstrap/*.yaml "${applications[@]}"; then
    status=1
  fi

  # Stream folders: only the files ArgoCD syncs. scripts/streams.py checks the rest.
  shopt -s nullglob
  stream_files=(streams/*/*/kafkatopic.yaml streams/*/*/kafkausers.yaml)
  shopt -u nullglob
  if [ ${#stream_files[@]} -gt 0 ]; then
    echo "==> streams/"
    if ! kubeconform "${kubeconform_args[@]}" "${stream_files[@]}"; then
      status=1
    fi
  fi
fi

exit "$status"
