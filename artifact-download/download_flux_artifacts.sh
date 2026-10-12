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
# Output: one flat folder with a single copy of each artifact.
#   <out>/<chart>-<version>.tgz        Helm charts, named as the chart packages them
#   <out>/<repository>-<tag>.<ext>     OCI artifacts, named after the repository and tag
#   <out>/manifest.tsv                 which source uses which file
# Sources that use identical artifacts share one file. Different artifacts that
# would get the same name are told apart by a short digest suffix.
#
# Requirements: kubectl, jq, curl, and sha256sum or shasum.

set -Eeuo pipefail
# Never exit silently: report the line and command of any unexpected failure.
on_error() {
  echo "error: line $2: \"$3\" failed (exit $1). Re-run with --debug for a full trace." >&2
}
trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR

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
      --debug             print every command as it runs
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
    --debug)          set -x; shift ;;
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
      if [[ "$handled" != "$token" ]]; then
        still+=("$obj")
      fi
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
  if [[ ${#objs[@]} -eq 0 ]]; then
    return 0
  fi

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
  echo "Connecting to source-controller (port-forward to svc/source-controller in $FLUX_NS on port $PORT)..."
  "${KUBECTL[@]}" -n "$FLUX_NS" port-forward svc/source-controller "$PORT:80" >"$TMP/port-forward.log" 2>&1 &
  PF_PID=$!
  BASE_URL="http://127.0.0.1:$PORT"
  code="000"
  for _ in $(seq 1 60); do
    if ! kill -0 "$PF_PID" 2>/dev/null; then
      echo "error: port-forward to svc/source-controller in $FLUX_NS failed:" >&2
      cat "$TMP/port-forward.log" >&2
      exit 1
    fi
    code="$(curl -s --max-time 2 -o /dev/null -w '%{http_code}' "$BASE_URL/" || true)"
    if [[ "$code" != "000" ]]; then
      break
    fi
    sleep 0.25
  done
  if [[ "$code" == "000" ]]; then
    echo "error: source-controller did not answer on $BASE_URL within 15s:" >&2
    cat "$TMP/port-forward.log" >&2
    exit 1
  fi
else
  echo "Using source-controller at $BASE_URL"
fi

# --- Download ----------------------------------------------------------------
mkdir -p "$OUT"
MANIFEST="$OUT/manifest.tsv"
printf 'kind\tnamespace\tname\trevision\tdigest\torigin\tfile\n' >"$MANIFEST"

if [[ -n "$NAMESPACE" ]]; then NS_ARGS=(-n "$NAMESPACE"); else NS_ARGS=(-A); fi
saved=0; shared=0; failed=0; skipped=0

record() {  # record FILE: add the current source to the manifest
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$kind" "$ns" "$name" "$revision" "$digest" "$origin" "$1" >>"$MANIFEST"
}

IFS=',' read -r -a kinds <<<"$KINDS"
for kind in "${kinds[@]}"; do
  case "$kind" in
    helmchart) resource="helmcharts.source.toolkit.fluxcd.io" ;;
    oci)       resource="ocirepositories.source.toolkit.fluxcd.io" ;;
    bucket)    resource="buckets.source.toolkit.fluxcd.io" ;;
    *) echo "error: unknown kind '$kind' (use helmchart, oci, bucket)" >&2; exit 2 ;;
  esac

  if ! "${KUBECTL[@]}" get "$resource" "${NS_ARGS[@]}" -o json >"$TMP/list.json" 2>"$TMP/err"; then
    echo "warning: cannot list $resource: $(tail -1 "$TMP/err")" >&2
    continue
  fi
  total="$(jq '.items | length' "$TMP/list.json")"
  echo "Found $total ${resource%%.*}${NAMESPACE:+ in $NAMESPACE}"
  if [[ "$total" -eq 0 ]]; then
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
    url_file="$(basename "${path%%\?*}")"
    case "$url_file" in
      *.tar.gz) ext=".tar.gz" ;;
      *.*)      ext=".${url_file##*.}" ;;
      *)        ext="" ;;
    esac
    # Flux v1 sources report "sha256:<hex>"; older ones only a bare SHA-256 "checksum".
    expected="${digest#sha256:}"
    if [[ "$digest" == *:* && "$digest" != sha256:* ]]; then
      expected=""
    fi

    # A readable file name: charts already have one; OCI artifacts are named after
    # the repository and tag; anything else after the source and its digest.
    case "$kind" in
      helmchart)
        file="$url_file" ;;
      oci)
        version="${revision%%@*}"
        if [[ "$revision" != *@* || -z "$version" ]]; then
          version="$(printf '%s' "${revision##*:}" | cut -c1-12)"
        fi
        file="${origin##*/}-${version}${ext}" ;;
      *)
        file="${name}-$(printf '%s' "${revision##*:}" | cut -c1-12)${ext}" ;;
    esac
    stem="${file%"$ext"}"

    # Already saved with the same content: just record that this source uses it.
    if [[ -f "$OUT/$file" && -n "$expected" && "$(sha256 "$OUT/$file")" == "$expected" ]]; then
      record "$file"
      echo "same  $label  $revision  -> $file (already saved)"
      shared=$((shared + 1)); continue
    fi

    part="$TMP/download.part"
    if ! curl -fsS --connect-timeout 10 --max-time 900 --retry 3 --retry-delay 1 \
        -o "$part" "$BASE_URL$path" 2>"$TMP/err"; then
      echo "FAIL  $label: download failed: $(tail -1 "$TMP/err")" >&2
      rm -f "$part"; failed=$((failed + 1)); continue
    fi
    actual="$(sha256 "$part")"
    if [[ -n "$expected" && "$actual" != "$expected" ]]; then
      echo "FAIL  $label: digest mismatch (expected $expected, got $actual)" >&2
      rm -f "$part"; failed=$((failed + 1)); continue
    fi
    if [[ -z "$digest" ]]; then
      echo "warn  $label: source reports no digest, keeping unverified" >&2
    elif [[ -z "$expected" ]]; then
      echo "warn  $label: cannot verify ${digest%%:*} digest, keeping unverified" >&2
    fi

    # Same name but different content: keep both, the new one with a digest suffix.
    if [[ -f "$OUT/$file" && "$(sha256 "$OUT/$file")" != "$actual" ]]; then
      file="${stem}-$(printf '%s' "$actual" | cut -c1-12)${ext}"
    fi
    if [[ -f "$OUT/$file" ]]; then
      rm -f "$part"
      record "$file"
      echo "same  $label  $revision  -> $file (already saved)"
      shared=$((shared + 1)); continue
    fi

    mv "$part" "$OUT/$file"
    record "$file"
    echo "ok    $label  $revision  -> $file"
    saved=$((saved + 1))
  done <"$TMP/rows"
done

echo
echo "Saved $saved file(s) to $OUT; $shared source(s) reused an already-saved file" \
     "($skipped skipped, $failed failed). Manifest: $MANIFEST"
if [[ "$failed" -gt 0 ]]; then
  exit 1
fi
