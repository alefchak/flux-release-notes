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
- It saves everything into one folder, with a single copy of each artifact (see [Output](#output)).

## Output

```
flux-artifacts/
├── cert-manager-v1.16.1.tgz          Helm charts, named as the chart packages them
├── redis-20.1.0.tgz                  one file, even if several deployments use it
├── redis-20.1.0-766e358ef2e5.tgz     same name, different content: digest suffix
├── web-manifests-2026.10.2.tar.gz    OCI artifacts: <repository>-<tag>
└── manifest.tsv                      which source uses which file
```

- **Shared charts:** sources that use an identical artifact share one file, and `manifest.tsv` lists each source against the file it uses.
- **Name clashes:** if two different artifacts would get the same name (for example a forked chart with the same version), the second gets a short digest suffix, so nothing is overwritten.
- **Re-runs:** running again into the same folder only downloads what's new or changed.

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
| `--debug` | Print every command as it runs, for troubleshooting |

## Requirements

`kubectl`, `jq`, `curl`, and `sha256sum` or `shasum`. The script avoids Bash 4
features so it can run on macOS's default Bash, though that hasn't been tested yet.

The kubectl identity needs:

- `get`/`list` on the source kinds it downloads.
- `get` on services and pods, and `create` on `pods/portforward`, in `flux-system`.
- For `--reconcile` only: `patch` on HelmCharts, OCIRepositories and the repositories charts come from (HelmRepositories, GitRepositories, Buckets).
