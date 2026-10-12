#!/usr/bin/env bash
#
# Download the Helm charts and OCI artifacts that Flux's source-controller has
# fetched, straight from the cluster.
#
# source-controller stores every artifact it fetches and serves it over HTTP at the
# URL in each source's .status.artifact.url. This script lists HelmCharts and
# OCIRepositories (optionally GitRepositories and Buckets too), downloads each
# artifact through a kubectl port-forward, verifies it against
# .status.artifact.digest, and writes a manifest of what it saved.
#
# Output layout:
#   <out>/helmcharts/<namespace>/<name>/<chart>-<version>.tgz
#   <out>/ocirepositories/<namespace>/<name>/<digest>.tar.gz
#   <out>/manifest.tsv   kind, namespace, name, revision, digest, origin, file
#
# Requirements: kubectl, jq, curl, and sha256sum or shasum.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: download_flux_artifacts.sh [options]

  -o, --output DIR        where to save artifacts (default: ./flux-artifacts)
      --context NAME      kubectl context to use
      --kubeconfig PATH   kubeconfig to use
  -n, --namespace NS      only sources in this namespace (default: all namespaces)
      --kinds LIST        comma-separated: helmchart,oci,git,bucket (default: helmchart,oci)
      --flux-namespace NS namespace where source-controller runs (default: flux-system)
      --port PORT         local port for the port-forward (default: 9797)
      --base-url URL      reach source-controller directly instead of port-forwarding,
                          e.g. http://source-controller.flux-system.svc when run in-cluster
  -h, --help              show this help
EOF
}

OUT="flux-artifacts"
NAMESPACE=""
KINDS="helmchart,oci"
FLUX_NS="flux-system"
PORT="9797"
BASE_URL=""
KUBECTL=(kubectl)

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output)      OUT="$2"; shift 2 ;;
    --context)        KUBECTL+=(--context "$2"); shift 2 ;;
    --kubeconfig)     KUBECTL+=(--kubeconfig "$2"); shift 2 ;;
    -n|--namespace)   NAMESPACE="$2"; shift 2 ;;
    --kinds)          KINDS="$2"; shift 2 ;;
    --flux-namespace) FLUX_NS="$2"; shift 2 ;;
    --port)           PORT="$2"; shift 2 ;;
    --base-url)       BASE_URL="${2%/}"; shift 2 ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for tool in kubectl jq curl; do
  command -v "$tool" >/dev/null || { echo "error: $tool is required" >&2; exit 1; }
done
if command -v sha256sum >/dev/null; then
  sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null; then
  sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  echo "error: sha256sum or shasum is required" >&2; exit 1
fi

TMP="$(mktemp -d)"
PF_PID=""
cleanup() {
  if [[ -n "$PF_PID" ]]; then kill "$PF_PID" 2>/dev/null || true; wait "$PF_PID" 2>/dev/null || true; fi
  rm -rf "$TMP"
}
trap cleanup EXIT

# --- Connect to source-controller -------------------------------------------
if [[ -z "$BASE_URL" ]]; then
  "${KUBECTL[@]}" -n "$FLUX_NS" port-forward svc/source-controller "$PORT:80" >"$TMP/port-forward.log" 2>&1 &
  PF_PID=$!
  BASE_URL="http://127.0.0.1:$PORT"
  for _ in $(seq 1 40); do
    if ! kill -0 "$PF_PID" 2>/dev/null; then
      echo "error: port-forward to svc/source-controller in $FLUX_NS failed:" >&2
      cat "$TMP/port-forward.log" >&2
      exit 1
    fi
    code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/" || true)"
    [[ "$code" != "000" ]] && break
    sleep 0.25
  done
  [[ "$code" == "000" ]] && { echo "error: source-controller did not answer on $BASE_URL" >&2; exit 1; }
fi

# --- Download ----------------------------------------------------------------
mkdir -p "$OUT"
MANIFEST="$OUT/manifest.tsv"
printf 'kind\tnamespace\tname\trevision\tdigest\torigin\tfile\n' >"$MANIFEST"

if [[ -n "$NAMESPACE" ]]; then NS_ARGS=(-n "$NAMESPACE"); else NS_ARGS=(-A); fi
ok=0; failed=0; skipped=0

IFS=',' read -r -a kinds <<<"$KINDS"
for kind in "${kinds[@]}"; do
  case "$kind" in
    helmchart) resource="helmcharts.source.toolkit.fluxcd.io";      dir="helmcharts" ;;
    oci)       resource="ocirepositories.source.toolkit.fluxcd.io"; dir="ocirepositories" ;;
    git)       resource="gitrepositories.source.toolkit.fluxcd.io"; dir="gitrepositories" ;;
    bucket)    resource="buckets.source.toolkit.fluxcd.io";         dir="buckets" ;;
    *) echo "error: unknown kind '$kind' (use helmchart, oci, git, bucket)" >&2; exit 2 ;;
  esac

  if ! "${KUBECTL[@]}" get "$resource" "${NS_ARGS[@]}" -o json >"$TMP/list.json" 2>"$TMP/err"; then
    echo "warning: cannot list $resource: $(tail -1 "$TMP/err")" >&2
    continue
  fi

  # One row per source, fields separated by the ASCII unit separator (a tab would let
  # `read` merge empty fields): namespace, name, artifact URL, revision, digest, origin.
  # origin is where the artifact came from: chart name for HelmCharts, URL otherwise.
  jq -r '.items[] | [
      .metadata.namespace,
      .metadata.name,
      (.status.artifact.url // ""),
      (.status.artifact.revision // ""),
      (.status.artifact.digest // .status.artifact.checksum // ""),
      (if .spec.chart then "\(.spec.sourceRef.kind)/\(.spec.sourceRef.name): \(.spec.chart)"
       else (.spec.url // (.spec.endpoint // "") + "/" + (.spec.bucketName // "")) end)
    ] | join("\u001f")' "$TMP/list.json" >"$TMP/rows"

  while IFS=$'\x1f' read -r ns name url revision digest origin; do
    label="$kind $ns/$name"
    if [[ -z "$url" ]]; then
      echo "skip  $label: no artifact yet (source not ready)" >&2
      skipped=$((skipped + 1)); continue
    fi

    path="/${url#*://*/}"                 # drop scheme and host: /helmchart/ns/name/file.tgz
    file="$(basename "${path%%\?*}")"
    dest="$OUT/$dir/$ns/$name/$file"
    mkdir -p "$(dirname "$dest")"

    if ! curl -fsS --retry 3 --retry-delay 1 -o "$dest.part" "$BASE_URL$path" 2>"$TMP/err"; then
      echo "FAIL  $label: download failed: $(tail -1 "$TMP/err")" >&2
      rm -f "$dest.part"; failed=$((failed + 1)); continue
    fi

    # Flux v1 sources report "sha256:<hex>"; older ones only a bare SHA-256 "checksum".
    expected="${digest#sha256:}"
    if [[ -n "$digest" && "$digest" != sha256:* && "$digest" == *:* ]]; then
      echo "warn  $label: cannot verify ${digest%%:*} digest, keeping unverified" >&2
    elif [[ -z "$expected" ]]; then
      echo "warn  $label: source reports no digest, keeping unverified" >&2
    else
      actual="$(sha256 "$dest.part")"
      if [[ "$actual" != "$expected" ]]; then
        echo "FAIL  $label: digest mismatch (expected $expected, got $actual)" >&2
        rm -f "$dest.part"; failed=$((failed + 1)); continue
      fi
    fi

    mv "$dest.part" "$dest"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$kind" "$ns" "$name" "$revision" "$digest" "$origin" \
      "${dest#"$OUT"/}" >>"$MANIFEST"
    echo "ok    $label  $revision  -> ${dest#"$OUT"/}"
    ok=$((ok + 1))
  done <"$TMP/rows"
done

echo
echo "Downloaded $ok artifact(s) to $OUT ($skipped skipped, $failed failed). Manifest: $MANIFEST"
[[ "$failed" -eq 0 ]]
