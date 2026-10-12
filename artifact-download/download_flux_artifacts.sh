#!/usr/bin/env bash
#
# Download the Helm charts and OCI artifacts that Flux's source-controller has
# fetched, straight from the cluster.
#
# source-controller stores every artifact it fetches and serves it over HTTP at the
# URL in each source's .status.artifact.url. This script lists HelmCharts and
# OCIRepositories (optionally Buckets too), downloads each
# artifact through a kubectl port-forward, verifies it against
# .status.artifact.digest, and writes a manifest of what it saved.
#
# With --reconcile, sources that are not Ready are asked to reconcile first (the
# same annotation `flux reconcile source` sets). For HelmCharts, the HelmRepository
# or GitRepository they come from is reconciled before the chart. The script waits
# up to --timeout seconds for Flux to handle each request, then downloads.
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
      --kinds LIST        comma-separated: helmchart,oci,bucket (default: helmchart,oci)
      --flux-namespace NS namespace where source-controller runs (default: flux-system)
      --port PORT         local port for the port-forward (default: 9797)
      --base-url URL      reach source-controller directly instead of port-forwarding,
                          e.g. http://source-controller.flux-system.svc when run in-cluster
      --reconcile         ask Flux to reconcile sources that are not Ready, and wait
                          for them before downloading (needs patch on the sources)
      --timeout SECONDS   how long to wait for each reconcile round (default: 180)
  -h, --help              show this help
EOF
}

OUT="flux-artifacts"
NAMESPACE=""
KINDS="helmchart,oci"
FLUX_NS="flux-system"
PORT="9797"
BASE_URL=""
RECONCILE=false
TIMEOUT=180
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
    --reconcile)      RECONCILE=true; shift ;;
    --timeout)        TIMEOUT="$2"; shift 2 ;;
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

READY='(.status.conditions // [] | map(select(.type == "Ready"))[0] // {})'

resource_of() {  # Flux source kind -> kubectl resource name
  case "$1" in
    HelmChart)      echo helmcharts.source.toolkit.fluxcd.io ;;
    HelmRepository) echo helmrepositories.source.toolkit.fluxcd.io ;;
    GitRepository)  echo gitrepositories.source.toolkit.fluxcd.io ;;
    OCIRepository)  echo ocirepositories.source.toolkit.fluxcd.io ;;
    Bucket)         echo buckets.source.toolkit.fluxcd.io ;;
    *) return 1 ;;
  esac
}

# reconcile_and_wait RESOURCE NAMESPACE/NAME...
# Requests a reconcile of each object and waits until Flux has handled every request
# (status.lastHandledReconcileAt equals the requested token) or TIMEOUT expires.
reconcile_and_wait() {
  local resource="$1"; shift
  local token obj handled deadline
  local pending=() still=()
  token="$(date -u +%Y-%m-%dT%H:%M:%SZ)-$$-$RANDOM"
  for obj in "$@"; do
    if "${KUBECTL[@]}" -n "${obj%%/*}" annotate --overwrite "$resource" "${obj#*/}" \
        "reconcile.fluxcd.io/requestedAt=$token" >/dev/null 2>"$TMP/err"; then
      echo "sync  ${resource%%.*} $obj: reconcile requested"
      pending+=("$obj")
    else
      echo "warn  ${resource%%.*} $obj: cannot request reconcile: $(tail -1 "$TMP/err")" >&2
    fi
  done
  deadline=$(( $(date +%s) + TIMEOUT ))
  while [[ ${#pending[@]} -gt 0 && $(date +%s) -lt $deadline ]]; do
    sleep 1
    still=()
    for obj in "${pending[@]}"; do
      handled="$("${KUBECTL[@]}" -n "${obj%%/*}" get "$resource" "${obj#*/}" \
        -o jsonpath='{.status.lastHandledReconcileAt}' 2>/dev/null || true)"
      [[ "$handled" == "$token" ]] || still+=("$obj")
    done
    pending=(${still[@]+"${still[@]}"})
  done
  for obj in ${pending[@]+"${pending[@]}"}; do
    echo "warn  ${resource%%.*} $obj: reconcile not handled within ${TIMEOUT}s" >&2
  done
}

# reconcile_not_ready KIND RESOURCE LIST_JSON: reconcile every source in the list
# that is not Ready (HelmCharts: their upstream source first).
reconcile_not_ready() {
  local kind="$1" resource="$2" list="$3" obj up_kind up_resource
  local objs=() ups=()
  local not_ready=".items[] | select((.spec.suspend // false) == false and $READY.status != \"True\")"
  jq -r ".items[] | select(.spec.suspend == true and $READY.status != \"True\")
      | \"warn  $kind \\(.metadata.namespace)/\\(.metadata.name): suspended, not reconciling\"" "$list" >&2
  while read -r obj; do objs+=("$obj"); done < <(jq -r "$not_ready
      | \"\\(.metadata.namespace)/\\(.metadata.name)\"" "$list")
  [[ ${#objs[@]} -eq 0 ]] && return 0

  if [[ "$kind" == helmchart ]]; then
    # Reconcile the repositories the charts come from first, one batch per kind.
    for up_kind in HelmRepository GitRepository Bucket OCIRepository; do
      ups=()
      while read -r obj; do ups+=("$obj"); done < <(jq -r --arg k "$up_kind" "$not_ready
          | select(.spec.sourceRef.kind == \$k)
          | \"\\(.metadata.namespace)/\\(.spec.sourceRef.name)\"" "$list" | sort -u)
      if [[ ${#ups[@]} -gt 0 ]]; then
        up_resource="$(resource_of "$up_kind")"
        reconcile_and_wait "$up_resource" "${ups[@]}"
      fi
    done
  fi
  reconcile_and_wait "$resource" "${objs[@]}"
}

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
    bucket)    resource="buckets.source.toolkit.fluxcd.io";         dir="buckets" ;;
    *) echo "error: unknown kind '$kind' (use helmchart, oci, bucket)" >&2; exit 2 ;;
  esac

  if ! "${KUBECTL[@]}" get "$resource" "${NS_ARGS[@]}" -o json >"$TMP/list.json" 2>"$TMP/err"; then
    echo "warning: cannot list $resource: $(tail -1 "$TMP/err")" >&2
    continue
  fi
  if $RECONCILE; then
    reconcile_not_ready "$kind" "$resource" "$TMP/list.json"
    "${KUBECTL[@]}" get "$resource" "${NS_ARGS[@]}" -o json >"$TMP/list.json"
  fi

  # One row per source, fields separated by the ASCII unit separator (a tab would let
  # `read` merge empty fields): namespace, name, artifact URL, revision, digest, origin,
  # Ready status and Ready message.
  # origin is where the artifact came from: chart name for HelmCharts, URL otherwise.
  jq -r '.items[] | [
      .metadata.namespace,
      .metadata.name,
      (.status.artifact.url // ""),
      (.status.artifact.revision // ""),
      (.status.artifact.digest // .status.artifact.checksum // ""),
      (if .spec.chart then "\(.spec.sourceRef.kind)/\(.spec.sourceRef.name): \(.spec.chart)"
       else (.spec.url // (.spec.endpoint // "") + "/" + (.spec.bucketName // "")) end),
      ('"$READY"'.status // "Unknown"),
      (('"$READY"'.message // "") | gsub("[\\n\\t]"; " "))
    ] | join("\u001f")' "$TMP/list.json" >"$TMP/rows"

  while IFS=$'\x1f' read -r ns name url revision digest origin ready message; do
    label="$kind $ns/$name"
    reason="not ready${message:+: $message}"
    if [[ -z "$url" ]]; then
      echo "skip  $label: no artifact ($reason)" >&2
      skipped=$((skipped + 1)); continue
    fi
    if [[ "$ready" != "True" ]]; then
      echo "warn  $label: $reason; downloading its last good artifact" >&2
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
