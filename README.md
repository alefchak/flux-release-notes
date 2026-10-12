# flux-release-notes

Generates release notes from the container images running in a Kubernetes
cluster whose workloads are deployed by [FluxCD](https://fluxcd.io).

It is built for multi-repo Flux setups, where a root repo syncs in repos owned by
other teams. It never reads a Git repo. Everything comes from the cluster, so it
works however each team lays out its repository.

## How it works

Flux labels every object it applies with the Kustomization or HelmRelease that
applied it. The script follows that trail:

```
workload ──labels──▶ HelmRelease / Kustomization
HelmRelease ──labels──▶ Kustomization that applied it
Kustomization ──spec.sourceRef──▶ GitRepository / OCIRepository / Bucket   (the team repo)
Kustomization ──labels──▶ parent Kustomization … up to the root
```

The release notes group images by the repo that delivers them. Each component
shows its full delivery path from the root Kustomization.

## Requirements

- Python 3.8+ (standard library only)
- `kubectl` with read access to:
  - Deployments, StatefulSets, DaemonSets, CronJobs, Pods
  - Kustomizations and HelmReleases
  - GitRepositories, OCIRepositories, Buckets, HelmRepositories

## Usage

```bash
# Baseline
./flux_release_notes.py --snapshot-out snapshots/r1.json --out RELEASE_NOTES.md

# Next release: diff against the previous snapshot
./flux_release_notes.py --previous snapshots/r1.json \
    --snapshot-out snapshots/r2.json --out RELEASE_NOTES.md --title "Release 2026.10.2"
```

With `--previous`, the notes start with a **Changes since previous release** section:

- Source revisions: each repo whose commit changed, with a compare link for
  GitHub, GitLab and Bitbucket
- Image changes (updated / added / removed), grouped by repo
- Helm chart version changes

| Option | Purpose |
|---|---|
| `--context`, `--kubeconfig` | Which cluster to read |
| `-n`, `--namespace` | Limit workloads to one namespace |
| `--exclude-namespace` | Skip a namespace (repeatable; defaults to the `kube-*` namespaces) |
| `--include-unmanaged` | Also list workloads without Flux labels |
| `--previous` | Previous snapshot JSON to diff against |
| `--snapshot-out` | Where to write this run's snapshot |
| `--out` | Markdown output file (default: stdout) |
| `--title` | Heading for the notes |

## Running in GitLab CI

`.gitlab-ci.yml` runs the script and keeps snapshots in the project's generic
package registry, so no extra tokens are needed (it uses `CI_JOB_TOKEN`).

| Pipeline | What happens |
|---|---|
| Tag (e.g. `v2026.10.2`) | Notes diffed against the last release, a GitLab Release is created with them, and the baseline snapshot is updated |
| Schedule or manual run | Preview notes against the last release, saved as job artifacts; baseline unchanged (set `UPDATE_BASELINE=true` to override) |

**Cluster access**, pick one:

- **GitLab agent for Kubernetes:** set the CI/CD variable `KUBE_CONTEXT` to
  `<agent-project-path>:<agent-name>`, and authorise this project under
  `ci_access` in the agent's config.
- **Kubeconfig:** add a CI/CD variable named `KUBECONFIG` of type *File*.

Other variables: `KUBECTL_VERSION`, and `EXTRA_ARGS` for extra script flags
(e.g. `--include-unmanaged`).

**First run:** start a manual pipeline with `UPDATE_BASELINE=true` to record
the baseline. Tag pipelines after that produce notes with a changes section.

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
  - apiGroups: [apps]
    resources: [deployments, statefulsets, daemonsets]
    verbs: [get, list]
  - apiGroups: [batch]
    resources: [cronjobs]
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

Bind it with a ClusterRoleBinding to whatever identity the CI job uses: the
service account in your kubeconfig, or the GitLab agent's CI identity if the
agent uses `access_as: ci_job` impersonation.

## Notes

- If a container is running at more than one digest (a rollout in progress, or
  drift), it is flagged in the notes.
- Workloads created by operators rather than by Flux carry no Flux labels. They
  are skipped unless you pass `--include-unmanaged`.
