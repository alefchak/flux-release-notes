# Flux artifact download

`download_flux_artifacts.sh` saves the Helm charts and OCI artifacts that Flux's
source-controller has already fetched. It downloads them from source-controller
inside the cluster, not from upstream registries, so you get exactly the bytes
Flux deployed. Use it to archive a release or to feed an air-gapped mirror.

```bash
./download_flux_artifacts.sh -o flux-artifacts            # Helm charts and OCI artifacts
./download_flux_artifacts.sh --kinds helmchart,oci,git    # also Git source tarballs
./download_flux_artifacts.sh -n payments --context prod   # one namespace, one cluster
```

How it works:

- It lists HelmCharts and OCIRepositories (`--kinds` can add `git` and `bucket`).
- It downloads each artifact from the URL Flux recorded (`.status.artifact.url`), through a `kubectl port-forward` to `svc/source-controller`. When running inside the cluster, pass `--base-url http://source-controller.flux-system.svc` to skip the port-forward.
- It checks each file's SHA-256 against `.status.artifact.digest` and discards any that don't match.
- It writes everything to `<out>/<kind>/<namespace>/<name>/`, plus a `manifest.tsv` listing kind, namespace, name, revision, digest, origin and file.

Sources that aren't ready yet are skipped. The script exits non-zero if any
download or digest check fails.

Requirements: `kubectl`, `jq`, `curl`, and `sha256sum` or `shasum`. The kubectl
identity needs:

- `get`/`list` on the source kinds it downloads.
- `get` on services and pods in `flux-system`.
- `create` on `pods/portforward` in `flux-system`.
