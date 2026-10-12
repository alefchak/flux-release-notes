# Flux artifact download

`download_flux_artifacts.sh` saves the Helm charts and OCI artifacts that Flux's
source-controller has already fetched. It downloads them from source-controller
inside the cluster, not from upstream registries, so you get exactly the bytes
Flux deployed. Use it to archive a release or to feed an air-gapped mirror.

```bash
./download_flux_artifacts.sh -o flux-artifacts            # Helm charts and OCI artifacts
./download_flux_artifacts.sh --reconcile                  # refresh sources that aren't ready first
./download_flux_artifacts.sh -n payments --context prod   # one namespace, one cluster
```

## How it works

- It lists HelmCharts and OCIRepositories (`--kinds bucket` adds Buckets).
- It downloads each artifact from the URL Flux recorded (`.status.artifact.url`), through a `kubectl port-forward` to `svc/source-controller`. When running inside the cluster, pass `--base-url http://source-controller.flux-system.svc` to skip the port-forward.
- It checks each file's SHA-256 against `.status.artifact.digest` and discards any that don't match.
- It writes everything to `<out>/<kind>/<namespace>/<name>/`, plus a `manifest.tsv` listing kind, namespace, name, revision, digest, origin and file.

The script exits non-zero if any download or digest check fails.

## Sources that aren't ready

| Situation | Without `--reconcile` | With `--reconcile` |
|---|---|---|
| Not ready, no artifact yet | Skipped | Reconciled, then downloaded if it recovers |
| Not ready, but has an earlier artifact | Last good artifact downloaded, with a warning | Reconciled first; if it still fails, the last good artifact is downloaded, with a warning |
| Suspended | Skipped | Skipped (Flux ignores reconcile requests for suspended sources) |

`--reconcile` sets the same annotation as `flux reconcile source`
(`reconcile.fluxcd.io/requestedAt`). For HelmCharts it reconciles the
HelmRepository (or other source) the chart comes from first, then the chart. It
waits up to `--timeout` seconds (default 180) for Flux to handle each round,
and warns about any request that wasn't handled in time.

## Options

| Option | Purpose |
|---|---|
| `-o`, `--output` | Where to save artifacts (default `./flux-artifacts`) |
| `--context`, `--kubeconfig` | Which cluster to use |
| `-n`, `--namespace` | Only sources in this namespace |
| `--kinds` | `helmchart`, `oci`, `bucket`, comma-separated (default `helmchart,oci`) |
| `--reconcile`, `--timeout` | Reconcile sources that aren't ready before downloading |
| `--flux-namespace` | Where source-controller runs (default `flux-system`) |
| `--port` | Local port for the port-forward (default `9797`) |
| `--base-url` | Reach source-controller directly instead of port-forwarding |

## Requirements

`kubectl`, `jq`, `curl`, and `sha256sum` or `shasum`. The script avoids Bash 4
features so it can run on macOS's default Bash, though that hasn't been tested yet.

The kubectl identity needs:

- `get`/`list` on the source kinds it downloads.
- `get` on services and pods, and `create` on `pods/portforward`, in `flux-system`.
- For `--reconcile` only: `patch` on HelmCharts, OCIRepositories and the repositories charts come from (HelmRepositories, GitRepositories, Buckets).
