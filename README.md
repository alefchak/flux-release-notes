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

## Notes

- If a container is running at more than one digest (a rollout in progress, or
  drift), it is flagged in the notes.
- Workloads created by operators rather than by Flux carry no Flux labels. They
  are skipped unless you pass `--include-unmanaged`.
