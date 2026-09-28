#!/usr/bin/env bash
# Release bot: deploys the newest image of each service to dev.
#
# For each apps/<service>/overlays/dev, finds the newest commit on the service
# repository's main branch that has an image on GHCR. CI only pushes an image
# after the tests pass. If that commit is newer than the deployed newTag, the
# bot opens a pull request that bumps newTag, validates it and merges it.
#
# Add apps/<service>/overlays/dev/.release-hold to pause a service.
#
# Usage: scripts/release-bot.sh [service]
#   With no service, releases every service. A failure in one service does not
#   stop the others.
#
# Env:   GH_TOKEN  token with contents and pull-requests write on this repo
#        DRY_RUN=1 print what would be deployed without changing anything
# Needs: git, gh, yq, curl, kustomize, kubeconform.
set -euo pipefail

self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")/.."

base=main
commits_to_scan=30

if [ $# -eq 0 ]; then
  failed=()
  shopt -s nullglob
  for dir in apps/*/overlays/dev/; do
    service="${dir#apps/}"
    service="${service%%/*}"
    # One process per service, so `set -e` still applies inside each release.
    if ! "$self" "$service"; then
      failed+=("$service")
    fi
  done
  if [ ${#failed[@]} -gt 0 ]; then
    echo "::error::Release failed for: ${failed[*]}"
    exit 1
  fi
  exit 0
fi

service="$1"
dir="apps/${service}/overlays/dev"
file="${dir}/kustomization.yaml"

log() { echo "${service}: $*"; }

if [ ! -f "$file" ]; then
  log "not deployed to dev yet, skipping"
  exit 0
fi
if [ -f "${dir}/.release-hold" ]; then
  log "held (${dir}/.release-hold), skipping"
  exit 0
fi

image=$(yq '.images[] | select(.name == "app") | .newName' "$file")
current=$(yq '.images[] | select(.name == "app") | .newTag' "$file")
repository="${image#ghcr.io/}"   # camnoss/<service>

target=""
for sha in $(gh api "repos/${repository}/commits?sha=main&per_page=${commits_to_scan}" --jq '.[].sha'); do
  if [ "$sha" = "$current" ]; then
    break
  fi
  if scripts/image-exists.sh "$image" "$sha"; then
    target="$sha"
    break
  fi
done

if [ -z "$target" ]; then
  log "up to date (${current:0:7})"
  exit 0
fi

short="${target:0:7}"
branch="chore/bump-${service}-${short}"

if [ -n "$(gh pr list --head "$branch" --state open --json number --jq '.[].number')" ]; then
  log "a pull request from ${branch} is already open, skipping"
  exit 0
fi

log "deploying ${short} (running ${current:0:7})"
if [ -n "${DRY_RUN:-}" ]; then
  exit 0
fi

git fetch -q origin "$base"
# -f drops anything a failed release of another service left behind.
git checkout -q -f -B "$branch" "origin/${base}"

SHA="$target" yq -i '
  (.images[] | select(.name == "app") | .newTag) = strenv(SHA) |
  (.images[] | select(.name == "app") | .newTag) style="double"
' "$file"

scripts/validate.sh "$dir"

git add "$file"
git commit -q -m "chore(${service}): deploy ${short} to dev"
git push -q --force origin "$branch"

url=$(gh pr create --base "$base" --head "$branch" \
  --title "chore(${service}): deploy ${short} to dev" \
  --body "Deploys **${service}** \`${short}\` to the \`${service}-dev\` namespace.

- Commit: https://github.com/${repository}/commit/${target}
- Changes since the running version: https://github.com/${repository}/compare/${current}...${target}

Opened and merged by the release bot after \`scripts/validate.sh\` passed. To pause deployments of ${service}, add \`${dir}/.release-hold\`.")

git checkout -q --detach "origin/${base}"

# Mergeability is computed right after the PR opens; retry briefly.
for attempt in 1 2 3; do
  if gh pr merge "$url" --squash; then
    break
  fi
  if [ "$attempt" = 3 ]; then
    exit 1
  fi
  sleep 5
done

git push -q origin --delete "$branch"
log "deployed ${short}: ${url}"
