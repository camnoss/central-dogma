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
   git checkout -b feat/add-external-secrets
   ```
2. Make your changes and validate them locally:
   ```bash
   kustomize build apps/<service>/overlays/dev | kubeconform -strict -summary
   ```
3. Open a pull request against `main`. CI validates the manifests.
4. After approval and merge, ArgoCD syncs the change.

### Branch naming

| Prefix | Use |
|---|---|
| `feat/` | New addon, tenant or application |
| `fix/` | Fix a broken or misconfigured resource |
| `chore/` | Version bumps and maintenance |
| `promote/` | Promote a release between environments |


## Principles

- **Git is the source of truth.** No `kubectl apply` against the cluster, except the initial bootstrap.
- **Everything through pull requests.** Every change is reviewed, validated and traceable.
- **Secure by default.** Every tenant starts isolated; access is granted, not assumed.
- **Platform as a product.** Development teams are our users; their friction is our backlog.