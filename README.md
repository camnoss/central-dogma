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

## Event streams

Teams publish **ECST state streams**: a compacted Kafka topic with one full snapshot of an entity per key, CloudEvents 1.0.2 headers (Kafka binary mode) and an Avro value whose contract lives in the schema registry. A stream is a folder; nobody files a ticket.

```text
platform/event-streams/              # Platform Team
├── application.yaml                 # AppProject + ApplicationSet event-streams
├── strimzi-operator/                # Strimzi 1.3 (Helm), sync wave -1
├── kafka-cluster/                   # nerv-kafka, KRaft, sync wave 0
│   ├── base/                        # production sizing: 3 brokers, RF 3, min ISR 2
│   └── overlays/dev/                # local Geofront: 1 broker, RF 1 (deployed today)
└── apicurio-registry/               # nerv-registry, Apicurio Registry 3, sync wave 1
streams/<domain>/<entity>/           # the domain team (CODEOWNERS)
├── kafkatopic.yaml                  # <domain>.<entity>.state.v1, compacted
├── kafkausers.yaml                  # one producer with Write, one user per consumer
├── schema/<Entity>State.avsc        # registry artifact <domain>/<entity>-state
├── asyncapi.yaml
└── catalog-info.yaml                # API entity in MAGI
```

### Creating a stream

Run **Publish an ECST state stream** in MAGI and define the schema's fields in the form. It opens a pull request from `feature/stream-<domain>-<entity>` with the folder.

**New streams merge themselves.** When **Stream gate** passes, the `merge-new-stream` job squash-merges the pull request if it only adds one new `streams/<domain>/<entity>/` folder that matches its branch name and doesn't exist on `main` yet. Any other pull request that touches `streams/`, such as a schema change, a new consumer or a pull request opened by hand, waits for a review by the domain team. After the merge:

1. The `event-streams` ApplicationSet creates `stream-<domain>-<entity>`, which syncs only `kafkatopic.yaml` and `kafkausers.yaml` into `kafka`. The `event-streams` AppProject allows nothing but `KafkaTopic` and `KafkaUser` there.
2. The **Streams** workflow registers the schema in `nerv-registry`. A merge made by `merge-new-stream` uses `GITHUB_TOKEN`, which does not trigger the push to `main`, so the job starts that run itself with `workflow_dispatch`.
3. MAGI discovers `catalog-info.yaml` and shows the stream as an `API` with its owner, producer and consumers.

Topics never set `replicas` or `min.insync.replicas`: they inherit the broker defaults of the environment (3 and 2 in production, 1 and 1 in dev), so the same folder works everywhere.

**Topics are never deleted from Git.** Stream Applications do not prune, the ApplicationSet never deletes the Applications it created, and every `KafkaTopic` carries `argocd.argoproj.io/sync-options: Delete=false`. Retiring a stream is a manual operation by the Platform Team.

### Consuming a stream

Open a pull request that adds a `KafkaUser` to the stream's `kafkausers.yaml`, with `Read` and `Describe` on the topic and `Read` on a consumer group of the same name (see `streams/billing/customer-bill`). The producing team reviews it. The User Operator writes the user's client certificate to a Secret of the same name in `kafka`; mount it in the consumer. Then add the API to `consumesApis` in the consumer's own `catalog-info.yaml`.

Clients read schemas from the registry and never register them: set `auto.register.schemas=false`. Record keys are plain UTF-8 strings, never schema-encoded.

### Stream gate

`scripts/streams.py` runs on every pull request through the **Streams** workflow. For each changed stream folder it checks:

1. The `.avsc` parses as Avro.
2. The schema is compatible with **every** registered version of its artifact (`FULL_TRANSITIVE`), using a dry run of `POST /apis/registry/v3/groups/{group}/artifacts/{artifact}/versions?dryRun=true`. Nothing is registered.
3. `asyncapi.yaml` references the folder's `.avsc` and passes `asyncapi validate`.
4. Exactly one `KafkaUser` has `Write` on the topic, the topic has `cleanup.policy: compact`, every Strimzi resource uses `kafka.strimzi.io/v1`, and no other stream folder declares a topic or user with the same name (both would sync the same resource).

These checks replace an admission policy. The workflow reports them as a single **Stream gate** check. On every push to `main`, the `register` job registers each schema with `ifExists=FIND_OR_CREATE_VERSION&canonical=true`, so a schema that is already registered keeps its version.

Run the local checks before you push (needs Python with `scripts/requirements-streams.txt` and the AsyncAPI CLI):

```bash
python scripts/streams.py lint streams/billing/customer-bill
```

> [!WARNING]
> **TODO: Stream gate does not block merges yet.** Making it a required status check on `main` would also block release bot pull requests, which never run workflows (see above). Pick one before relying on the gate: a branch ruleset that requires **Stream gate** with the release bot as a bypass actor, or a separate token for the bot so its pull requests run workflows.

### Registry access

`nerv-registry` serves the Core API v3 at `/apis/registry/v3` and the Confluent-compatible API at `/apis/ccompat/v7` on `http://nerv-registry-app-service.kafka.svc:8080`. Reads are anonymous. Writes need a Kubernetes ServiceAccount token, which the registry validates with the TokenReview API:

| Identity | Registry role | Used by |
|---|---|---|
| `schema-registry-writers/central-dogma-schema-writer` | developer (register) | `register` job on `main` |
| `kafka/central-dogma-schema-gate` | read-only (dry runs) | pull request checks |

No identity has the admin role. The global rules (`COMPATIBILITY=FULL_TRANSITIVE`, `VALIDITY=FULL`) are set in `apicurio-registry/registry.yaml` and change only through a pull request. Group concatenation is on, so the ccompat subject `<group>:<artifactId>` (for example `billing:customer-bill-state`) maps to the artifact that CI registers.

Kubernetes creates both tokens. Copy them into GitHub once, and again after rotating them:

```bash
token() { kubectl get secret "$2" -n "$1" -o jsonpath='{.data.token}' | base64 -d; }

gh secret set SCHEMA_REGISTRY_URL -R camnoss/central-dogma --body "<registry URL reachable from the runner>"
gh secret set SCHEMA_REGISTRY_GATE_TOKEN -R camnoss/central-dogma \
  --body "$(token kafka central-dogma-schema-gate-token)"
# The writer token goes in the schema-registry environment. Limit that
# environment to the main branch in Settings > Environments.
gh secret set SCHEMA_REGISTRY_WRITE_TOKEN -R camnoss/central-dogma --env schema-registry \
  --body "$(token schema-registry-writers central-dogma-schema-writer-token)"
```

The registry is only reachable inside Geofront. The registry jobs run on the runner named by the `SCHEMA_REGISTRY_RUNNER` repository variable (default `ubuntu-latest`), which must reach `SCHEMA_REGISTRY_URL`: a self-hosted runner in Geofront, or a GitHub-hosted runner once the registry is exposed.

To check the global rules:

```bash
kubectl -n kafka port-forward svc/nerv-registry-app-service 8081:8080
curl -s http://localhost:8081/apis/registry/v3/admin/rules                    # ["COMPATIBILITY","VALIDITY"]
curl -s http://localhost:8081/apis/registry/v3/admin/rules/COMPATIBILITY      # {"config":"FULL_TRANSITIVE",...}
```


## Principles

- **Git is the source of truth.** No `kubectl apply` against the cluster, except the initial bootstrap.
- **Everything through pull requests.** Every change is validated and traceable. People review platform changes; the release bot merges `dev` deployments on its own.
- **Secure by default.** Every tenant starts isolated; access is granted, not assumed.
- **Platform as a product.** Development teams are our users; their friction is our backlog.