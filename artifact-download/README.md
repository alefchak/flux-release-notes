# Flux artifact download

`download_flux_artifacts.sh` saves the Helm charts and OCI artifacts that Flux
has **actually deployed**. It downloads them from source-controller inside the
cluster, not from upstream registries, so you get exactly the bytes Flux used.
Use it to archive a release or to feed an air-gapped mirror.

```bash
./download_flux_artifacts.sh -o flux-artifacts            # everything that is deployed
./download_flux_artifacts.sh --reconcile                  # refresh stale sources first
./download_flux_artifacts.sh -n payments --context prod   # one namespace, one cluster
```

## Why it doesn't just download every source

A source's `.status.artifact.url` is the newest artifact Flux has fetched, which
isn't always what's deployed:

- **Source ahead of the deployment:** a HelmChart fetched a newer chart, but the HelmRelease upgrade is pending or failing.
- **Source behind the deployment:** the source is failing and still holds an older "last good" artifact.
- **Leftover sources:** a HelmChart or OCIRepository that nothing uses any more still holds an old version.

Downloading every source's artifact therefore sometimes gives you the wrong
versions. This script starts from the deployments instead.

## How it works

1. **Find what's deployed:**
   - For every HelmRelease, the chart version Helm installed (`.status.history[0]`) and the HelmChart or OCIRepository it came from.
   - For every Kustomization built from an OCIRepository, its `.status.lastAppliedRevision`.
2. **Check the source:** the source's artifact must be that same version. For OCI charts, the digest must match too.
3. **Download:** fetch the artifact through a `kubectl port-forward` to `svc/source-controller`, and check its SHA-256 against `.status.artifact.digest`. When running inside the cluster, `--base-url http://source-controller.flux-system.svc` skips the port-forward.
4. **Report anything else:**
   - A mismatch is reported and not downloaded.
   - A missing source is reported as a failure.
   - Sources that nothing uses are listed at the end, and not downloaded.

The script exits non-zero if anything is mismatched or fails.

## Output

```
flux-artifacts/
├── cert-manager-v1.16.1.tgz          Helm charts, named as the chart packages them
├── redis-20.1.0.tgz                  one file, even if several deployments use it
├── redis-20.1.0-766e358ef2e5.tgz     same name, different content: digest suffix
├── web-manifests-2026.10.2.tar.gz    OCI artifacts: <repository>-<tag>
└── manifest.tsv                      which HelmRelease / Kustomization uses which file
```

- **Shared charts:** deployments that use an identical artifact share one file.
- **Name clashes:** if two different artifacts would get the same name (for example a forked chart with the same version), the second gets a short digest suffix, so nothing is overwritten.
- **Re-runs:** running again into the same folder only downloads what's new or changed.

## Mismatches and `--reconcile`

| Situation | Without `--reconcile` | With `--reconcile` |
|---|---|---|
| Source behind the deployment, or failing | Reported, not downloaded | Source (and, for a HelmChart, its repository) reconciled, then checked again |
| Source ahead (HelmRelease upgrade pending or failing) | Reported, not downloaded | Still reported: reconciling the source can't bring back the older version. Fix or finish the HelmRelease upgrade, then re-run |
| Source not ready, but its artifact matches | Downloaded, with a warning | Reconciled, then downloaded |
| Suspended source | Used as is | Not reconciled (Flux ignores the request) |

`--reconcile` sets the same annotation as `flux reconcile source`
(`reconcile.fluxcd.io/requestedAt`). It waits up to `--timeout` seconds (default
180) for Flux to handle each round, and warns about any request that wasn't
handled in time.

## Options

| Option | Purpose |
|---|---|
| `-o`, `--output` | Where to save artifacts (default `./flux-artifacts`) |
| `--context`, `--kubeconfig` | Which cluster to use |
| `-n`, `--namespace` | Only HelmReleases and Kustomizations in this namespace |
| `--reconcile`, `--timeout` | Refresh sources that aren't ready or don't match, then check again |
| `--flux-namespace` | Where source-controller runs (default `flux-system`) |
| `--port` | Local port for the port-forward (default `9797`) |
| `--base-url` | Reach source-controller directly instead of port-forwarding |
| `--debug` | Print every command as it runs, for troubleshooting |

## Requirements

`kubectl`, `jq`, `curl`, and `sha256sum` or `shasum`. The script avoids Bash 4
features so it can run on macOS's default Bash, though that hasn't been tested yet.

The kubectl identity needs:

- `get`/`list` on HelmReleases, Kustomizations, HelmCharts and OCIRepositories.
- `get` on services and pods, and `create` on `pods/portforward`, in `flux-system`.
- For `--reconcile` only: `patch` on HelmCharts, OCIRepositories and the repositories charts come from (HelmRepositories, GitRepositories, Buckets).
