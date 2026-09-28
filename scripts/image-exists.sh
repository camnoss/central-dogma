#!/usr/bin/env bash
# Exits 0 if <image>:<tag> exists on GHCR, 1 if it does not.
#
# Usage: scripts/image-exists.sh ghcr.io/camnoss/greeting <tag>
#
# Uses an anonymous pull token, so it only sees public images.
# Needs curl and yq on PATH.
set -euo pipefail

image="$1"
tag="$2"

repository="${image#ghcr.io/}"
if [ "$repository" = "$image" ]; then
  echo "Only ghcr.io images are supported, got: ${image}" >&2
  exit 2
fi

token=$(curl -fsS "https://ghcr.io/token?scope=repository:${repository}:pull" | yq -p json '.token')

status=$(curl -sS -o /dev/null -w '%{http_code}' --head \
  -H "Authorization: Bearer ${token}" \
  -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json' \
  "https://ghcr.io/v2/${repository}/manifests/${tag}")

[ "$status" = 200 ]
