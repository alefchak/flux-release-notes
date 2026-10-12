# flux-release-notes

Generates release notes for a Kubernetes cluster whose workloads are deployed by
[FluxCD](https://fluxcd.io).

It is built for multi-repo Flux setups, where a root repo syncs in repos owned by
other teams. It never reads a Git repo. Everything comes from the cluster, so it
works however each team lays out its repository.

## Output

There are three formats. Choose one for `--out` with `--format`, or write the
others alongside it with `--inventory-out` and `--detailed-out`.

| Format | Contents |
|---|---|
| `summary` (default) | **What changed:** each repo's version change with a compare link, followed by the charts and images that changed with it. **Needs attention:** one line per problem, such as a failing Kustomization, a re-pushed tag or a missing digest. **Deployed:** tables of the repos, Helm charts and images now running |
| `inventory` | Only the Deployed tables: what is running right now, with no comparison and no warnings |
| `detailed` | Every source, Kustomization, HelmRelease and container, with delivery paths |

The image table gives each image's full name, tag, full digest and the
workloads using it. The snapshot JSON always holds everything listed below.

## What gets recorded

| Area | Recorded |
|---|---|
| **Images** | Every container (including init containers and sidecars) as a full reference: `registry/repository:tag@sha256:digest`. Short names like `redis:7` are expanded to `docker.io/library/redis:7`. The notes also say where the digest came from: pinned in the manifest, reported by running pods, or looked up in the registry. |
| **Helm charts** | Every HelmRelease: chart name, deployed version, app version, the version constraint requested, chart source and its URL (plus the chart path for charts in Git), OCI chart digest, and the Helm release name and namespace. |
| **Git sources** | Every GitRepository: URL, the ref it tracks (branch, tag, semver range or commit), the branch or tag that resolved to, and the full commit SHA, with a link to the commit. |
| **Other sources** | OCIRepositories (URL, ref, resolved tag, digest), Buckets, HelmRepositories (URL, type). |
| **Kustomizations** | Source, path and the revision last applied. |
| **Cluster** | kubectl context, Kubernetes version, Flux version. |

Credentials embedded in source URLs (for example `https://oauth2:TOKEN@host/...`)
are removed before anything is written.

### Warnings

The notes have a Warnings section that lists:

- Flux objects that are not Ready or are suspended.
- Kustomizations whose last applied revision is behind their source.
- Images whose digest is unknown, and why.
- Pods running different digests for the same tag (the tag was re-pushed).
- Pods still running an older spec during a rollout.

## How workloads are traced

```
Pod ──ownerReferences──▶ ReplicaSet / Job ──▶ Deployment / CronJob / … ──▶ operator CR
workload (or its CR owner) ──Flux labels──▶ HelmRelease / Kustomization
HelmRelease ──labels──▶ Kustomization that applied it
Kustomization ──spec.sourceRef──▶ GitRepository / OCIRepository / Bucket   (the team repo)
Kustomization ──labels──▶ parent Kustomization … up to the root
```

Images are read from two places:

- **Pod templates** (Deployments, StatefulSets, DaemonSets, CronJobs, Jobs). This covers workloads with no pods, such as idle CronJobs or deployments scaled to zero.
- **Pods.** This covers pods that operators create from custom resources, and gives the digest each container is actually running.

Images are grouped by the repo that delivers them, and each one shows its delivery
path from the root Kustomization.

## Requirements

- Python 3.8+ (standard library only)
- `kubectl` with read access (see [RBAC](#minimal-read-only-rbac))
- Optional: `crane` or `skopeo`, for `--resolve-digests`

## Usage

```bash
# Baseline
./flux_release_notes.py --release v1 --snapshot-out snapshots/v1.json --out RELEASE_NOTES.md

# Next release: diff against the previous snapshot
./flux_release_notes.py --release v2 --previous snapshots/v1.json \
    --snapshot-out snapshots/v2.json --images-out images.txt --out RELEASE_NOTES.md
```

With `--previous`, the notes start with a **Changes since …** section. It lists:

- **Sources:** each source whose URL, ref or commit changed, with a compare link.
- **Helm charts:** each chart added, removed or changed, by chart name, version, app version, digest or source.
- **Images:** each image added, updated or removed, grouped by the repo that delivers it.

| Option | Purpose |
|---|---|
| `--context`, `--kubeconfig` | Which cluster to read |
| `-n`, `--namespace` | Limit workloads to one namespace |
| `--exclude-namespace` | Skip a namespace (repeatable; defaults to the `kube-*` namespaces) |
| `--include-unmanaged` | Also list workloads that no Flux object applied |
| `--resolve-digests auto\|crane\|skopeo` | Look up digests in the registry for images no pod reports one for |
| `--forge HOST=github\|gitlab\|bitbucket` | Commit and compare links for a self-hosted Git server |
| `--flux-namespace` | Where Flux is installed (default `flux-system`) |
| `--release` | Name of this release, stored in the snapshot |
| `--previous` | Previous snapshot JSON to diff against |
| `--snapshot-out` | Where to write this run's snapshot (JSON with everything above) |
| `--images-out` | Plain list of unique full image references, e.g. for scanners |
| `--out` | Markdown output file (default: stdout) |
| `--title` | Heading for the notes |
| `--format summary\|inventory\|detailed` | Format for `--out` (default `summary`) |
| `--inventory-out`, `--detailed-out` | Also write the inventory or detailed report to these files |

### About digests

Pods report the digest of the image the node pulled, usually the multi-arch
index digest. `--resolve-digests` asks the registry for the same kind of digest.
For private registries it needs credentials: `crane` and `skopeo` both read
`~/.docker/config.json`, and `skopeo` also honours `REGISTRY_AUTH_FILE`.

## Running in GitLab CI

`.gitlab-ci.yml` runs the script and keeps snapshots in the project's generic
package registry. It authenticates with the built-in `CI_JOB_TOKEN`, so you
don't need to create any tokens.

| Pipeline | What happens |
|---|---|
| Tag (e.g. `v2026.10.2`) | Notes diffed against the last release, a GitLab Release is created with the summary, and the baseline snapshot is updated. `INVENTORY.md` and `RELEASE_NOTES_DETAILED.md` are kept as job artifacts |
| Schedule or manual run | Preview notes against the last release, saved as job artifacts; baseline unchanged (set `UPDATE_BASELINE=true` to override) |

Retrying a tag pipeline still diffs against the release before it.

**Cluster access.** Pick one:

- **GitLab agent for Kubernetes:** set the CI/CD variable `KUBE_CONTEXT` to `<agent-project-path>:<agent-name>`, and authorise this project under `ci_access` in the agent's config.
- **Kubeconfig:** add a CI/CD variable named `KUBECONFIG` of type *File*.

**Registry access (optional).** Add a *File* variable called `REGISTRY_AUTH_FILE`
containing a Docker `config.json`. You only need it for private images that no
pod reports a digest for.

**Other variables:**

| Variable | Purpose |
|---|---|
| `KUBECTL_VERSION` | kubectl version to install |
| `RESOLVE_DIGESTS` | Default `skopeo`; set `off` to skip registry lookups |
| `FORGES` | Space-separated `host=type` pairs for self-hosted Git servers |
| `EXTRA_ARGS` | Extra flags for the script |

The job works on amd64 and arm64 runners.

**First run.** Start a manual pipeline with `UPDATE_BASELINE=true` to record the
baseline. Tag pipelines after that produce notes with a changes section.

**Package registry setting.** The baseline is re-uploaded under the same name for
each release, and GitLab returns the newest copy. If your group disallows duplicate
generic packages, allow duplicates for the `flux-release-notes` package name.

### Minimal read-only RBAC

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: flux-release-notes
rules:
  - apiGroups: [""]
    resources: [pods]
    verbs: [get, list]
  - apiGroups: [""]
    resources: [namespaces]
    resourceNames: [flux-system]
    verbs: [get]
  - apiGroups: [apps]
    resources: [deployments, statefulsets, daemonsets, replicasets]
    verbs: [get, list]
  - apiGroups: [batch]
    resources: [cronjobs, jobs]
    verbs: [get, list]
  - apiGroups: [kustomize.toolkit.fluxcd.io]
    resources: [kustomizations]
    verbs: [get, list]
  - apiGroups: [helm.toolkit.fluxcd.io]
    resources: [helmreleases]
    verbs: [get, list]
  - apiGroups: [source.toolkit.fluxcd.io]
    resources: [gitrepositories, ocirepositories, buckets, helmrepositories]
    verbs: [get, list]
```

If operators in your cluster create workloads from custom resources (database
operators, Argo Rollouts and so on), add `get` on those resource types too. The
script reads them to find the Flux labels. Without that access the workloads are
still listed, but they show as not managed by Flux.

Bind the role with a ClusterRoleBinding to whatever identity the CI job uses:
either the service account in your kubeconfig, or the GitLab agent's CI identity
if the agent uses `access_as: ci_job` impersonation.

## Limitations

- Workloads applied by hand or by plain `helm install` (not by Flux) are left out unless you pass `--include-unmanaged`.
- If a container runtime reports only a local image ID (common with images loaded onto nodes rather than pulled), the digest is unknown unless `--resolve-digests` can find it.
- Ephemeral debug containers are ignored.
