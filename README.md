# Central Dogma

> The core of Nerv. Everything running in Geofront is born here, via GitOps with ArgoCD.

**Central Dogma** is the GitOps source of truth for **Nerv**, an internal developer platform (IDP) built on Kubernetes, Backstage and ArgoCD. It declares the desired state of the platform: cluster addons, team tenants and application deployments. ArgoCD continuously reconciles the cluster against this repository.

If it is not in `main`, it does not exist in the cluster.

## Platform components

| Codename | Component | Description |
|---|---|---|
| **Nerv** | The platform | Internal developer platform for development teams |
| **Geofront** | Kubernetes cluster(s) | The foundation everything runs on |
| **Central Dogma** | This repository | GitOps source of truth |
| **MAGI** | Backstage | Developer portal: software catalog, templates and docs |
| **AT Field** | Security & isolation | RBAC, NetworkPolicies, ResourceQuotas, LimitRanges |
| **Eva Units** | Team workloads | Services deployed by development teams |


## Getting started

### Prerequisites

- [kind](https://kind.sigs.k8s.io/) (or k3d) for local clusters
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh/)
- [Kustomize](https://kustomize.io/) (bundled with `kubectl`)

### 1. Create a local Geofront

```bash
kind create cluster --name geofront
```

### 2. Install ArgoCD

This is the only manual installation step. From here on, ArgoCD manages itself.

```bash
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update
helm install argocd argo/argo-cd -n argocd --create-namespace
```

### 3. Bootstrap the platform

Apply the root application once. It points ArgoCD at `platform/` and `tenants/`, and everything else is reconciled from Git.

```bash
kubectl apply -n argocd -f bootstrap/
```

### 4. Access the ArgoCD UI

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d; echo

kubectl port-forward svc/argocd-server -n argocd 8080:443
```

Open https://localhost:8080 and log in as `admin`. All applications should reach **Synced / Healthy**.

## Workflow

Central Dogma follows **trunk-based development** with a single long-lived branch.

- `main` is the only long-lived branch and is what ArgoCD tracks.
- **Environments are directories, not branches.** Each environment is a Kustomize overlay under `apps/<service>/overlays/`.
- Changes go through short-lived branches and pull requests. Direct pushes to `main` are not allowed.

### Making a change

1. Create a short-lived branch from `main`:
   ```bash
   git checkout -b feature/add-external-secrets
   ```
2. Make your changes and validate them locally (needs `kustomize` and `kubeconform`):
   ```bash
   scripts/validate.sh                             # everything
   scripts/validate.sh apps/<service>/overlays/dev # one overlay
   ```
3. Open a pull request against `main`. The **Validate** workflow runs the same script.
4. After approval and merge, ArgoCD syncs the change within about a minute.

### Branch naming

| Prefix | Use |
|---|---|
| `feature/` | New addon, tenant or application |
| `fix/` | Fix a broken or misconfigured resource |
| `chore/` | Version bumps and maintenance (`chore/bump-<service>-<sha>` is the release bot) |
| `promote/` | Promote a release between environments |

## Automatic dev deployments

Services created from Eva Templates deploy to `dev` without anyone clicking merge.

- **First deploy.** MAGI opens a pull request from `feature/add-<service>`. Once it validates and the first image is on GHCR, the Validate workflow merges it. This only happens when the pull request adds a new `apps/<service>/` and changes nothing else.
- **Every merge after that.** Service CI pushes `ghcr.io/camnoss/<service>:<sha>` and sends an `image-published` dispatch to this repository. The **Release bot** workflow finds the newest commit on the service's `main` that has an image, then opens, validates and merges a pull request that bumps `newTag` in `apps/<service>/overlays/dev/kustomization.yaml`. An hourly run catches up on missed dispatches.

Expect new pods about 4–5 minutes after a merge in the service repository, most of it spent in the service's CI.

### Pausing and rolling back

- **Pause a service:** add an empty `apps/<service>/overlays/dev/.release-hold` through a pull request. The bot skips the service until the file is removed.
- **Roll back:** pause the service, then open a pull request that sets `newTag` back to a known good commit SHA.
- **Release now:** `gh workflow run release-bot.yaml -R camnoss/central-dogma` (optionally `-f service=<service>`).

### The dispatch token

Service CI sends the dispatch with `DOGMA_DISPATCH_TOKEN`, a fine-grained personal access token that only has **Contents: read and write** on this repository. MAGI stores it and copies it into every new service repository as an Actions secret. The dispatch only wakes the bot up; the bot decides what to deploy.

To rotate it, create a new token, update MAGI's `DOGMA_DISPATCH_TOKEN`, and re-set the secret on existing services:

```bash
for repo in greeting; do  # every service under apps/
  gh secret set DOGMA_DISPATCH_TOKEN -R "camnoss/${repo}" --body "$NEW_TOKEN"
done
```

Bot pull requests are opened with the workflow's `GITHUB_TOKEN`, which does not trigger other workflows, so the bot runs `scripts/validate.sh` itself. Do not make **Validate** a required status check on `main`: bot pull requests never report it and would never merge.


## Principles

- **Git is the source of truth.** No `kubectl apply` against the cluster, except the initial bootstrap.
- **Everything through pull requests.** Every change is validated and traceable. People review platform changes; the release bot merges `dev` deployments on its own.
- **Secure by default.** Every tenant starts isolated; access is granted, not assumed.
- **Platform as a product.** Development teams are our users; their friction is our backlog.