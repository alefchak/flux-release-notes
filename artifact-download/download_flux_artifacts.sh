#!/usr/bin/env bash
#
# Download the Helm charts and OCI artifacts that are actually deployed by Flux,
# straight from the source-controller in the cluster.
#
# Why not just download every source's .status.artifact.url? Because that is the
# newest artifact a source has fetched, not what is deployed. They differ when:
#   * a HelmChart fetched a newer chart but the HelmRelease upgrade is pending or failing,
#   * a source is failing and still holds an older "last good" artifact,
#   * a HelmChart or OCIRepository is left over and no longer used by anything.
#
# So this script starts from what is deployed:
#   * every HelmRelease: the chart version in .status.history[0] (what Helm installed),
#     and the HelmChart or OCIRepository it was installed from;
#   * every Kustomization built from an OCIRepository: .status.lastAppliedRevision.
# It downloads the source's artifact only when its revision matches what is
# deployed, verifies it against .status.artifact.digest, and reports any mismatch
# instead of saving the wrong version.
#
# With --reconcile, sources that are not Ready or do not match are asked to
# reconcile first (the annotation `flux reconcile source` sets); for HelmCharts the
# repository they come from is reconciled before the chart. Then everything is
# checked again.
#
# Output: one flat folder with a single copy of each artifact.
#   <out>/<chart>-<version>.tgz        Helm charts, named as the chart packages them
#   <out>/<repository>-<tag>.<ext>     OCI artifacts, named after the repository and tag
#   <out>/manifest.tsv                 which HelmRelease / Kustomization uses which file
#
# Requirements: kubectl, jq, curl, and sha256sum or shasum.

set -Eeuo pipefail
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
  -n, --namespace NS      only HelmReleases and Kustomizations in this namespace
      --flux-namespace NS namespace where source-controller runs (default: flux-system)
      --port PORT         local port for the port-forward (default: 9797)
      --base-url URL      reach source-controller directly instead of port-forwarding,
                          e.g. http://source-controller.flux-system.svc when run in-cluster
      --reconcile         ask Flux to reconcile sources that are not Ready or don't match
                          what is deployed, then check again (needs patch on the sources)
      --timeout SECONDS   how long to wait for each reconcile round (default: 180)
      --debug             print every command as it runs
  -h, --help              show this help
EOF
}

OUT="flux-artifacts"
NAMESPACE=""
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

resource_of() {  # Flux kind -> kubectl resource name
  case "$1" in
    HelmRelease)    echo helmreleases.helm.toolkit.fluxcd.io ;;
    Kustomization)  echo kustomizations.kustomize.toolkit.fluxcd.io ;;
    HelmChart)      echo helmcharts.source.toolkit.fluxcd.io ;;
    HelmRepository) echo helmrepositories.source.toolkit.fluxcd.io ;;
    GitRepository)  echo gitrepositories.source.toolkit.fluxcd.io ;;
    OCIRepository)  echo ocirepositories.source.toolkit.fluxcd.io ;;
    Bucket)         echo buckets.source.toolkit.fluxcd.io ;;
    *) return 1 ;;
  esac
}

fetch() {  # fetch KIND FILE: list all objects of KIND in every namespace
  local resource
  resource="$(resource_of "$1")"
  if ! "${KUBECTL[@]}" get "$resource" -A -o json >"$2" 2>"$TMP/err"; then
    echo "warning: cannot list $resource: $(tail -1 "$TMP/err")" >&2
    echo '{"items":[]}' >"$2"
  fi
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

# --- What is deployed, and which artifact each deployment came from ----------
# One row per HelmRelease / OCI-based Kustomization, fields separated by the ASCII
# unit separator (a tab would let `read` merge empty fields):
#   1 consumer, 2 source kind, 3 source namespace, 4 source name, 5 artifact URL,
#   6 revision, 7 digest, 8 origin, 9 Ready status, 10 Ready message, 11 suspended,
#   12 deployed version, 13 match (yes / no / unknown / nosource),
#   14 upstream kind, 15 upstream name, 16 upstream namespace
ROWS_JQ=$(cat <<'EOF'
def ready: ((.status.conditions // []) | map(select(.type == "Ready"))[0] // {});
def key: "\(.metadata.namespace)/\(.metadata.name)";
def index(l): reduce l[] as $o ({}; .[$o | key] = $o);
($hc[0].items | index(.)) as $charts
| ($oc[0].items | index(.)) as $ocis
| (
    ($hr[0].items[] | select($ns == "" or .metadata.namespace == $ns) | . as $o
      | ((($o.status.history // [])[0]) // {}) as $h
      | (if $o.spec.chartRef then
           {kind: $o.spec.chartRef.kind,
            key: "\($o.spec.chartRef.namespace // $o.metadata.namespace)/\($o.spec.chartRef.name)"}
         else
           {kind: "HelmChart",
            key: ($o.status.helmChart //
                  "\($o.spec.chart.spec.sourceRef.namespace // $o.metadata.namespace)/\($o.metadata.namespace)-\($o.metadata.name)")}
         end) as $ref
      | {consumer: "HelmRelease \($o | key)", ref: $ref, mode: "chart",
         want: ($h.chartVersion // $o.status.lastAppliedRevision // ""), want_digest: ($h.ociDigest // "")}),
    ($ks[0].items[] | select($ns == "" or .metadata.namespace == $ns)
      | select(.spec.sourceRef.kind == "OCIRepository") | . as $o
      | {consumer: "Kustomization \($o | key)", mode: "revision",
         ref: {kind: "OCIRepository",
               key: "\($o.spec.sourceRef.namespace // $o.metadata.namespace)/\($o.spec.sourceRef.name)"},
         want: ($o.status.lastAppliedRevision // ""), want_digest: ""})
  )
| . as $c
| (if $c.ref.kind == "HelmChart" then $charts[$c.ref.key]
   elif $c.ref.kind == "OCIRepository" then $ocis[$c.ref.key] else null end) as $s
| ($s.status.artifact.revision // "") as $rev
| ($rev | split("@")) as $parts
| (if $s == null then "nosource"
   elif $c.want == "" then "unknown"
   elif $c.mode == "revision" then (if $rev == $c.want then "yes" else "no" end)
   elif ($parts[0] == $c.want or ($parts[0] | startswith($c.want + "+")))
        and ($c.want_digest == "" or ($parts | length) < 2 or $parts[1] == $c.want_digest) then "yes"
   else "no" end) as $match
| [ $c.consumer, $c.ref.kind, ($c.ref.key | split("/")[0]), ($c.ref.key | split("/")[1]),
    ($s.status.artifact.url // ""), $rev,
    ($s.status.artifact.digest // $s.status.artifact.checksum // ""),
    (if $c.ref.kind == "HelmChart" then ($s.spec.chart // "") else ($s.spec.url // "") end),
    (($s // {}) | ready | .status // "Unknown"),
    ((($s // {}) | ready | .message // "") | gsub("[\\n\\t]"; " ")),
    (($s.spec.suspend // false) | tostring),
    $c.want, $match,
    ($s.spec.sourceRef.kind // ""), ($s.spec.sourceRef.name // ""), ($s.metadata.namespace // "")
  ] | join("\u001f")
EOF
)

# Sources that no HelmRelease or Kustomization uses (often the "old versions").
UNUSED_JQ=$(cat <<'EOF'
def key: "\(.metadata.namespace)/\(.metadata.name)";
([$rows[] | split("\u001f") | "\(.[1]) \(.[2])/\(.[3])"]) as $used
| ($hc[0].items[] | "HelmChart \(key)"), ($oc[0].items[] | "OCIRepository \(key)")
| select(. as $x | ($used | any(. == $x)) | not)
EOF
)

load_state() {
  fetch HelmRelease "$TMP/hr.json"
  fetch Kustomization "$TMP/ks.json"
  fetch HelmChart "$TMP/hc.json"
  fetch OCIRepository "$TMP/oc.json"
}

build_rows() {
  jq -nr --arg ns "$NAMESPACE" \
    --slurpfile hr "$TMP/hr.json" --slurpfile ks "$TMP/ks.json" \
    --slurpfile hc "$TMP/hc.json" --slurpfile oc "$TMP/oc.json" \
    "$ROWS_JQ" >"$TMP/rows"
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

# --- Work out what is deployed ----------------------------------------------
load_state
build_rows
echo "Found $(jq '.items | length' "$TMP/hr.json") HelmReleases and" \
     "$(jq '[.items[] | select(.spec.sourceRef.kind == "OCIRepository")] | length' "$TMP/ks.json")" \
     "OCI-based Kustomizations${NAMESPACE:+ (using those in $NAMESPACE)}"

if $RECONCILE; then
  # Sources that are not Ready or don't match the deployment, plus the repositories
  # their HelmCharts come from. Suspended sources ignore reconcile requests.
  awk -F $'\x1f' '($9 != "True" || $13 == "no") && $13 != "nosource" && $11 != "true" { print $2, $3 "/" $4 }' \
    "$TMP/rows" | sort -u >"$TMP/targets"
  awk -F $'\x1f' '($9 != "True" || $13 == "no") && $11 == "true" { print "warn  " $2 " " $3 "/" $4 ": suspended, not reconciling" }' \
    "$TMP/rows" | sort -u >&2
  awk -F $'\x1f' '($9 != "True" || $13 == "no") && $13 != "nosource" && $11 != "true" && $2 == "HelmChart" && $14 != "" { print $14, $16 "/" $15 }' \
    "$TMP/rows" | sort -u >"$TMP/upstreams"
  for kind in HelmRepository GitRepository Bucket OCIRepository; do
    objs=()
    while read -r k obj; do
      if [[ "$k" == "$kind" ]]; then objs+=("$obj"); fi
    done <"$TMP/upstreams"
    if [[ ${#objs[@]} -gt 0 ]]; then reconcile_and_wait "$(resource_of "$kind")" "${objs[@]}"; fi
  done
  for kind in HelmChart OCIRepository; do
    objs=()
    while read -r k obj; do
      if [[ "$k" == "$kind" ]]; then objs+=("$obj"); fi
    done <"$TMP/targets"
    if [[ ${#objs[@]} -gt 0 ]]; then reconcile_and_wait "$(resource_of "$kind")" "${objs[@]}"; fi
  done
  load_state
  build_rows
fi

# --- Download ----------------------------------------------------------------
mkdir -p "$OUT"
MANIFEST="$OUT/manifest.tsv"
printf 'deployed by\tsource\trevision\tdigest\tfile\n' >"$MANIFEST"
saved=0; shared=0; failed=0; mismatched=0; skipped=0

record() {  # record FILE: add the current deployment to the manifest
  printf '%s\t%s\t%s\t%s\t%s\n' "$consumer" "$source" "$revision" "$digest" "$1" >>"$MANIFEST"
}

while IFS=$'\x1f' read -r consumer skind sns sname url revision digest origin ready message \
    suspended want match _up_kind _up_name _up_ns; do
  source="$skind $sns/$sname"
  case "$match" in
    nosource)
      echo "FAIL  $consumer: its source $source does not exist" >&2
      failed=$((failed + 1)); continue ;;
    no)
      echo "MISMATCH  $consumer: deployed $want, but $source has ${revision:-no artifact} - not downloaded" >&2
      echo "          (the HelmRelease may be mid-upgrade or failing, or the source may be failing;" \
           "--reconcile can refresh the source)" >&2
      mismatched=$((mismatched + 1)); continue ;;
    unknown)
      echo "warn  $consumer: no deployed version recorded yet; using what $source has" >&2 ;;
  esac
  if [[ -z "$url" ]]; then
    echo "skip  $consumer: $source has no artifact (not ready${message:+: $message})" >&2
    skipped=$((skipped + 1)); continue
  fi
  if [[ "$ready" != "True" && "$suspended" != "true" ]]; then
    echo "warn  $consumer: $source is not ready${message:+ ($message)}, but its artifact matches what is deployed" >&2
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

  # A readable file name: charts already have one; OCI artifacts are named after the
  # repository and tag (or a short digest when there is no tag).
  if [[ "$skind" == HelmChart ]]; then
    file="$url_file"
  else
    version="${revision%%@*}"
    if [[ "$revision" != *@* || -z "$version" ]]; then
      version="$(printf '%s' "${revision##*:}" | cut -c1-12)"
    fi
    file="${origin##*/}-${version}${ext}"
  fi
  stem="${file%"$ext"}"

  # Already saved with the same content: just record that this deployment uses it.
  if [[ -f "$OUT/$file" && -n "$expected" && "$(sha256 "$OUT/$file")" == "$expected" ]]; then
    record "$file"
    echo "same  $consumer  $revision  -> $file (already saved)"
    shared=$((shared + 1)); continue
  fi

  part="$TMP/download.part"
  if ! curl -fsS --connect-timeout 10 --max-time 900 --retry 3 --retry-delay 1 \
      -o "$part" "$BASE_URL$path" 2>"$TMP/err"; then
    echo "FAIL  $consumer: download from $source failed: $(tail -1 "$TMP/err")" >&2
    rm -f "$part"; failed=$((failed + 1)); continue
  fi
  actual="$(sha256 "$part")"
  if [[ -n "$expected" && "$actual" != "$expected" ]]; then
    echo "FAIL  $consumer: digest mismatch from $source (expected $expected, got $actual)" >&2
    rm -f "$part"; failed=$((failed + 1)); continue
  fi
  if [[ -z "$digest" ]]; then
    echo "warn  $consumer: $source reports no digest, keeping unverified" >&2
  elif [[ -z "$expected" ]]; then
    echo "warn  $consumer: cannot verify ${digest%%:*} digest from $source, keeping unverified" >&2
  fi

  # Same name but different content: keep both, the new one with a digest suffix.
  if [[ -f "$OUT/$file" && "$(sha256 "$OUT/$file")" != "$actual" ]]; then
    file="${stem}-$(printf '%s' "$actual" | cut -c1-12)${ext}"
  fi
  if [[ -f "$OUT/$file" ]]; then
    rm -f "$part"
    record "$file"
    echo "same  $consumer  $revision  -> $file (already saved)"
    shared=$((shared + 1)); continue
  fi

  mv "$part" "$OUT/$file"
  record "$file"
  echo "ok    $consumer  $revision  -> $file"
  saved=$((saved + 1))
done <"$TMP/rows"

# Sources nothing uses: never downloaded, listed so leftovers are visible.
if [[ -z "$NAMESPACE" ]]; then
  jq -nr --rawfile r "$TMP/rows" --slurpfile hc "$TMP/hc.json" --slurpfile oc "$TMP/oc.json" \
    '($r | split("\n") | map(select(length > 0))) as $rows | '"$UNUSED_JQ" >"$TMP/unused"
  if [[ -s "$TMP/unused" ]]; then
    echo
    echo "Not used by any HelmRelease or Kustomization, so not downloaded:"
    sed 's/^/  /' "$TMP/unused"
  fi
fi

echo
echo "Saved $saved file(s) to $OUT; $shared deployment(s) reused an already-saved file;" \
     "$mismatched mismatched, $skipped skipped, $failed failed. Manifest: $MANIFEST"
if [[ "$failed" -gt 0 || "$mismatched" -gt 0 ]]; then
  exit 1
fi
