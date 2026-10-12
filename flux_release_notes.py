#!/usr/bin/env python3
"""
Generate release notes for a Kubernetes cluster whose workloads are deployed by
FluxCD, built for multi-repo setups (a root repo that syncs in other teams' repos).

Nothing is read from Git. Everything comes from the cluster, so it works however
each team lays out its repository.

Recorded for every release
  * Every container image as a full reference: registry/repository:tag@sha256:digest.
    The digest comes from the image reference itself (pinned), from what the
    running pods report, or, with --resolve-digests, from the registry.
  * Every HelmRelease: chart name, deployed chart version, app version, the
    version constraint requested, chart source URL and OCI chart digest.
  * Every Flux source: GitRepository URL, ref (branch/tag/semver/commit), the
    resolved tag or branch and full commit SHA; OCIRepository URL, tag and digest;
    Buckets and HelmRepositories.
  * Every Kustomization: source, path and the revision it last applied.

How workloads are traced to the repo that delivers them
  Pod --ownerReferences--> ReplicaSet/Job --> Deployment/CronJob/... --> any CR owner
  workload --Flux labels--> HelmRelease or Kustomization that applied it
  HelmRelease --labels--> Kustomization that applied it
  Kustomization --spec.sourceRef--> GitRepository / OCIRepository / Bucket
  Kustomization --labels--> parent Kustomization ... up to the root

Requirements: Python 3.8+, kubectl with read access to the cluster.
Optional: `crane` or `skopeo` on PATH for --resolve-digests.
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from urllib.parse import urlsplit, urlunsplit

SCHEMA = 2
TEMPLATE_RESOURCES = "deployments,statefulsets,daemonsets,cronjobs,jobs,replicasets"
DEFAULT_EXCLUDED_NAMESPACES = ["kube-system", "kube-public", "kube-node-lease"]

KS_NAME = "kustomize.toolkit.fluxcd.io/name"
KS_NS = "kustomize.toolkit.fluxcd.io/namespace"
HR_NAME = "helm.toolkit.fluxcd.io/name"
HR_NS = "helm.toolkit.fluxcd.io/namespace"

SOURCE_RESOURCES = {
    "GitRepository": "gitrepositories.source.toolkit.fluxcd.io",
    "OCIRepository": "ocirepositories.source.toolkit.fluxcd.io",
    "Bucket": "buckets.source.toolkit.fluxcd.io",
    "HelmRepository": "helmrepositories.source.toolkit.fluxcd.io",
}
# Kinds that sit between a Pod and the object a person actually manages.
INTERMEDIATE_KINDS = {"Pod", "ReplicaSet", "Job"}


# --------------------------------------------------------------------------- #
# kubectl
# --------------------------------------------------------------------------- #
class Kubectl:
    def __init__(self, context=None, kubeconfig=None):
        self.base = ["kubectl"]
        if kubeconfig:
            self.base += ["--kubeconfig", kubeconfig]
        if context:
            self.base += ["--context", context]

    def run(self, args, allow_fail=False):
        proc = subprocess.run(self.base + args, capture_output=True, text=True)
        if proc.returncode != 0:
            if allow_fail:
                return None
            sys.exit(f"kubectl {' '.join(args)} failed:\n{proc.stderr.strip()}")
        return proc.stdout

    def get_json(self, resource, namespace=None, allow_fail=False):
        args = ["get", resource, "-o", "json"]
        args += ["-n", namespace] if namespace else ["-A"]
        out = self.run(args, allow_fail=allow_fail)
        return json.loads(out).get("items", []) if out else []

    def get_one(self, resource, namespace, name):
        args = ["get", resource, name, "-o", "json"] + (["-n", namespace] if namespace else [])
        out = self.run(args, allow_fail=True)
        return json.loads(out) if out else None

    def current_context(self):
        out = self.run(["config", "current-context"], allow_fail=True)
        return out.strip() if out else "unknown"

    def server_version(self):
        out = self.run(["version", "-o", "json"], allow_fail=True)
        try:
            return json.loads(out)["serverVersion"]["gitVersion"]
        except (TypeError, ValueError, KeyError):
            return "unknown"


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def key(kind, ns, name):
    return f"{kind}|{ns}|{name}" if kind else ""


def pretty(k):
    if not k:
        return ""
    kind, ns, name = k.split("|")
    return f"{kind} {ns}/{name}"


def condition(status):
    for c in (status or {}).get("conditions", []) or []:
        if c.get("type") == "Ready":
            return c.get("status", "Unknown"), (c.get("message") or "").strip()
    return "Unknown", ""


def flux_owner(labels):
    labels = labels or {}
    if labels.get(HR_NAME):
        return key("HelmRelease", labels.get(HR_NS, ""), labels[HR_NAME])
    if labels.get(KS_NAME):
        return key("Kustomization", labels.get(KS_NS, ""), labels[KS_NAME])
    return ""


def ref_key(ref, default_ns):
    ref = ref or {}
    return key(ref.get("kind", ""), ref.get("namespace") or default_ns, ref.get("name", ""))


def clean_url(url):
    """Drop credentials embedded in a URL (https://user:token@host/...)."""
    if not url:
        return ""
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    if "@" not in p.netloc:
        return url
    host = p.netloc.rsplit("@", 1)[1]
    if p.scheme in ("http", "https", "oci", "s3", "gs"):
        return urlunsplit((p.scheme, host, p.path, p.query, p.fragment))
    # ssh://git@host/...: keep the user name, drop any password
    return urlunsplit((p.scheme, f"{p.username}@{host}" if p.username else host, p.path, p.query, p.fragment))


def parse_revision(rev):
    """Flux artifact revision -> (ref name, 'algo:hash').

    'main@sha1:abc..', 'v1.2.3@sha1:abc..', 'sha1:abc..', 'latest@sha256:..',
    and the legacy 'main/abc..' form."""
    rev = rev or ""
    m = re.match(r"^(?:(.*)@)?((?:sha1|sha256|sha384|sha512):[0-9a-f]+)$", rev)
    if m:
        return m.group(1) or "", m.group(2)
    m = re.match(r"^(.*)/([0-9a-f]{40})$", rev)
    if m:
        return m.group(1), f"sha1:{m.group(2)}"
    return rev, ""


def bare_hash(h):
    return h.split(":", 1)[1] if ":" in (h or "") else (h or "")


def fmt_ref(ref):
    return ", ".join(f"{k} `{v}`" for k, v in (ref or {}).items()) or "default"


def esc(s):
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def code(s):
    return f"`{esc(s)}`" if s else "-"


# --------------------------------------------------------------------------- #
# Image references
# --------------------------------------------------------------------------- #
def parse_image(image):
    """Normalise an image reference the way container runtimes do.

    'redis:7' -> docker.io / library/redis / 7
    'ghcr.io/acme/api:1.2@sha256:..' -> ghcr.io / acme/api / 1.2 / sha256:..
    """
    ref, digest = (image or "").strip(), ""
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    name, tag = ref, ""
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "docker.io", name
    if registry in ("index.docker.io", "registry-1.docker.io"):
        registry = "docker.io"
    if registry == "docker.io" and "/" not in repo:
        repo = f"library/{repo}"
    if not tag and not digest:
        tag = "latest"
    return {"registry": registry, "repository": repo, "tag": tag, "digest": digest}


def image_string(p, with_digest=True):
    s = f"{p['registry']}/{p['repository']}"
    if p["tag"]:
        s += f":{p['tag']}"
    if with_digest and p["digest"]:
        s += f"@{p['digest']}"
    return s


def parse_image_id(image_id):
    """containerStatus.imageID -> (registry digest, local image id).

    'docker-pullable://repo@sha256:..' / 'repo@sha256:..' carry a registry
    (manifest) digest. A bare 'sha256:..' is only the local image config ID,
    which cannot be used to pull the image, so it is kept separately."""
    iid = re.sub(r"^[a-z-]+://", "", image_id or "")
    if "@" in iid:
        return iid.split("@", 1)[1], ""
    if iid.startswith("sha256:"):
        return "", iid
    return "", ""


class DigestResolver:
    """Looks up the manifest digest of a tag in the registry (crane or skopeo)."""

    def __init__(self, mode):
        self.tool, self.cache = None, {}
        if mode in ("crane", "skopeo"):
            if not shutil.which(mode):
                sys.exit(f"--resolve-digests {mode}: '{mode}' is not on PATH")
            self.tool = mode
        elif mode == "auto":
            self.tool = next((t for t in ("crane", "skopeo") if shutil.which(t)), None)
            if not self.tool:
                print("warning: --resolve-digests auto: neither crane nor skopeo found; skipping", file=sys.stderr)

    def __call__(self, ref):
        if not self.tool:
            return "", "no crane/skopeo available"
        if ref not in self.cache:
            self.cache[ref] = self._lookup(ref)
        return self.cache[ref]

    def _lookup(self, ref):
        try:
            if self.tool == "crane":
                p = subprocess.run(["crane", "digest", ref], capture_output=True, text=True, timeout=90)
                if p.returncode == 0 and p.stdout.strip().startswith("sha256:"):
                    return p.stdout.strip(), ""
                return "", (p.stderr.strip().splitlines() or ["lookup failed"])[-1]
            p = subprocess.run(["skopeo", "inspect", "--raw", f"docker://{ref}"], capture_output=True, timeout=90)
            if p.returncode == 0 and p.stdout:
                return "sha256:" + hashlib.sha256(p.stdout).hexdigest(), ""
            return "", (p.stderr.decode(errors="replace").strip().splitlines() or ["lookup failed"])[-1]
        except subprocess.TimeoutExpired:
            return "", "registry lookup timed out"


# --------------------------------------------------------------------------- #
# Flux objects
# --------------------------------------------------------------------------- #
def collect_sources(kubectl):
    sources = {}
    for kind, resource in SOURCE_RESOURCES.items():
        for s in kubectl.get_json(resource, allow_fail=True):
            md, spec, st = s["metadata"], s.get("spec", {}), s.get("status", {})
            ready, msg = condition(st)
            revision = (st.get("artifact") or {}).get("revision", "")
            resolved, rev_hash = parse_revision(revision)
            url = spec.get("url", "")
            if kind == "Bucket":
                url = f"{spec.get('endpoint', '')}/{spec.get('bucketName', '')}"
            sources[key(kind, md["namespace"], md["name"])] = {
                "kind": kind,
                "url": clean_url(url),
                "ref": {k: v for k, v in (spec.get("ref") or {}).items() if v and isinstance(v, str)},
                "type": spec.get("type", ""),              # HelmRepository: "oci" or "default"
                "revision": revision,
                "resolved_ref": resolved,                  # branch / tag the revision points at
                "hash": rev_hash,                          # sha1:<commit> or sha256:<digest>
                "suspended": bool(spec.get("suspend")),
                "ready": ready,
                "message": msg if ready != "True" else "",
            }
    return sources


def collect_kustomizations(kubectl):
    out = {}
    for ks in kubectl.get_json("kustomizations.kustomize.toolkit.fluxcd.io", allow_fail=True):
        md, spec, st = ks["metadata"], ks.get("spec", {}), ks.get("status", {})
        ready, msg = condition(st)
        out[key("Kustomization", md["namespace"], md["name"])] = {
            "parent": flux_owner(md.get("labels")),
            "source": ref_key(spec.get("sourceRef"), md["namespace"]),
            "path": spec.get("path", "") or "./",
            "applied_revision": st.get("lastAppliedRevision", ""),
            "attempted_revision": st.get("lastAttemptedRevision", ""),
            "suspended": bool(spec.get("suspend")),
            "ready": ready,
            "message": msg if ready != "True" else "",
        }
    return out


def collect_helmreleases(kubectl):
    out = {}
    for hr in kubectl.get_json("helmreleases.helm.toolkit.fluxcd.io", allow_fail=True):
        md, spec, st = hr["metadata"], hr.get("spec", {}), hr.get("status", {})
        ready, msg = condition(st)
        chart_spec = (spec.get("chart") or {}).get("spec", {})
        if chart_spec:
            chart_source = ref_key(chart_spec.get("sourceRef"), md["namespace"])
            requested = chart_spec.get("version") or "*"
            chart_ref = chart_spec.get("chart", "")
        else:  # spec.chartRef -> OCIRepository or HelmChart
            chart_source = ref_key(spec.get("chartRef"), md["namespace"])
            requested, chart_ref = "", ""
        history = st.get("history") or []
        target_ns = spec.get("targetNamespace") or md["namespace"]
        default_release = f"{spec['targetNamespace']}-{md['name']}" if spec.get("targetNamespace") else md["name"]
        if history:  # helm.toolkit.fluxcd.io/v2 and v2beta2
            h = history[0]
            chart = h.get("chartName") or chart_ref
            version, app_version = h.get("chartVersion", ""), h.get("appVersion", "")
            chart_digest = h.get("ociDigest", "")
            release_name = h.get("name") or spec.get("releaseName") or default_release
            helm_status, deployed_at = h.get("status", ""), h.get("lastDeployed", "")
        else:        # v2beta1: lastAppliedRevision is the chart version
            chart = chart_ref
            version = st.get("lastAppliedRevision", "")
            app_version = chart_digest = deployed_at = ""
            release_name = spec.get("releaseName") or default_release
            helm_status = ""
        out[key("HelmRelease", md["namespace"], md["name"])] = {
            "parent": flux_owner(md.get("labels")),
            "source": chart_source,
            "chart": chart,
            "chart_path": chart_ref if chart_source.startswith("GitRepository|") else "",
            "chart_version": version,
            "app_version": app_version,
            "requested_version": requested,
            "chart_digest": chart_digest,
            "release_name": release_name,
            "target_namespace": target_ns,
            "helm_status": helm_status,
            "deployed_at": deployed_at,
            "attempted_revision": st.get("lastAttemptedRevision", ""),
            "suspended": bool(spec.get("suspend")),
            "ready": ready,
            "message": msg if ready != "True" else "",
        }
    return out


def resolve_delivery(flux, owner):
    """(source key of the repo that delivered it, chain of Flux keys root -> owner)."""
    chain, seen, cur, delivering = [], set(), owner, ""
    while cur and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        obj = flux.get(cur)
        if not obj:
            break
        if not delivering and cur.startswith("Kustomization|"):
            delivering = obj["source"]
        cur = obj["parent"]
    if not delivering and owner in flux:  # HelmRelease not applied by a Kustomization
        delivering = flux[owner]["source"]
    return delivering, list(reversed(chain))


# --------------------------------------------------------------------------- #
# Workloads and images
# --------------------------------------------------------------------------- #
def controller_ref(obj):
    refs = obj["metadata"].get("ownerReferences") or []
    return next((r for r in refs if r.get("controller")), refs[0] if refs else None)


class OwnerGraph:
    def __init__(self, kubectl, objs):
        self.kubectl = kubectl
        self.by_uid = {o["metadata"].get("uid"): o for o in objs}
        self.fetched = {}

    def _get(self, ns, ref):
        uid = ref.get("uid")
        if uid in self.by_uid:
            return self.by_uid[uid]
        if uid not in self.fetched:
            api = ref.get("apiVersion", "")
            group = api.split("/")[0] if "/" in api else ""
            resource = ref["kind"].lower() + (f".{group}" if group else "")
            obj = self.kubectl.get_one(resource, ns, ref["name"])
            if obj is None:  # no permission or gone: keep a stub so the chain still names it
                obj = {"kind": ref["kind"], "metadata": {"name": ref["name"], "namespace": ns,
                                                         "uid": uid, "labels": {}}}
            obj.setdefault("kind", ref["kind"])
            self.fetched[uid] = obj
        return self.fetched[uid]

    def chain(self, obj):
        """obj, its controller, that one's controller, ... (bottom -> top)."""
        out, seen = [obj], {obj["metadata"].get("uid")}
        while True:
            ref = controller_ref(out[-1])
            if not ref or ref.get("uid") in seen or ref.get("kind") == "Node":
                return out
            seen.add(ref.get("uid"))
            out.append(self._get(out[-1]["metadata"].get("namespace"), ref))

    def workload(self, obj):
        """The object a person manages: skip Pods/ReplicaSets/Jobs that have a controller."""
        chain = self.chain(obj)
        for i, o in enumerate(chain):
            if o["kind"] in INTERMEDIATE_KINDS and i + 1 < len(chain):
                continue
            return o, chain
        return chain[-1], chain


def pod_template_spec(obj):
    spec = obj.get("spec", {})
    if obj["kind"] == "CronJob":
        return spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
    if obj["kind"] == "Pod":
        return spec
    return spec.get("template", {}).get("spec", {})


def containers_of(spec):
    for c in spec.get("initContainers") or []:
        yield c, "sidecar" if c.get("restartPolicy") == "Always" else "init"
    for c in spec.get("containers") or []:
        yield c, "container"


def collect_images(kubectl, flux, namespace, excluded, include_unmanaged, resolver):
    objs = kubectl.get_json(TEMPLATE_RESOURCES, namespace)
    pods = kubectl.get_json("pods", namespace)
    graph = OwnerGraph(kubectl, objs)
    entries = {}

    def entry(workload, chain, cname, ctype, spec_image):
        md = workload["metadata"]
        k = (md["namespace"], workload["kind"], md["name"], cname)
        if k not in entries:
            owner = ""
            for o in reversed(chain):  # outermost Flux label wins
                owner = flux_owner(o["metadata"].get("labels"))
                if owner:
                    break
            top = chain[-1]
            source, flux_chain = resolve_delivery(flux, owner) if owner else ("", [])
            entries[k] = {
                "namespace": md["namespace"], "kind": workload["kind"], "name": md["name"],
                "controlled_by": f"{top['kind']}/{top['metadata']['name']}" if top is not workload else "",
                "container": cname, "container_type": ctype, "spec_image": spec_image,
                "owner": owner, "source": source, "chain": flux_chain,
                "_running": Counter(),
            }
        return entries[k]

    # Desired state: pod templates of workloads (covers scaled-to-zero and idle CronJobs).
    for obj in objs:
        if obj["metadata"]["namespace"] in excluded:
            continue
        if obj["kind"] in ("ReplicaSet", "Job") and controller_ref(obj):
            continue  # its Deployment / CronJob / CR owner provides the template
        workload, chain = graph.workload(obj)
        if workload is not obj:
            continue
        for c, ctype in containers_of(pod_template_spec(obj)):
            entry(workload, chain, c["name"], ctype, c.get("image", ""))

    # Actual state: what pods run (also covers pods created by operators/CRs).
    for pod in pods:
        if pod["metadata"]["namespace"] in excluded:
            continue
        workload, chain = graph.workload(pod)
        st = pod.get("status", {})
        statuses = {s.get("name"): s for s in
                    (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or [])}
        for c, ctype in containers_of(pod.get("spec", {})):
            e = entry(workload, chain, c["name"], ctype, c.get("image", ""))
            cs = statuses.get(c["name"], {})
            digest, local_id = parse_image_id(cs.get("imageID", ""))
            e["_running"][(c.get("image", ""), digest, local_id)] += 1

    results = []
    for e in entries.values():
        if not e["owner"] and not include_unmanaged:
            continue
        running = [{"image": img, "digest": d, "image_id": lid, "pods": n}
                   for (img, d, lid), n in sorted(e.pop("_running").items())]
        p = parse_image(e["spec_image"])
        current = [r for r in running if r["image"] == e["spec_image"]]
        stale = [r for r in running if r["image"] != e["spec_image"]]
        cur_digests = sorted({r["digest"] for r in current if r["digest"]})
        warnings, digest_source = [], ""
        if p["digest"]:
            digest_source = "pinned"
            if any(d != p["digest"] for d in cur_digests):
                warnings.append("running digest differs from the pinned digest")
        elif len(cur_digests) == 1:
            p["digest"], digest_source = cur_digests[0], "running pods"
        elif len(cur_digests) > 1:
            warnings.append(f"tag was re-pushed; pods run {len(cur_digests)} different digests ("
                            + ", ".join(d[7:19] for d in cur_digests) + ")")
        elif any(r["image_id"] for r in current):
            warnings.append("node reported no registry digest")
        if not p["digest"] and not cur_digests and resolver is not None:
            d, err = resolver(image_string(p, with_digest=False))
            if d:
                p["digest"], digest_source = d, "registry"
            else:
                warnings.append(f"registry lookup failed: {err}")
        if not p["digest"] and not warnings:
            warnings.append("digest unknown" + ("" if running else " (no pods running)"))
        if stale:
            n = sum(r["pods"] for r in stale)
            warnings.append(f"rollout in progress: {n} pod{'s' if n != 1 else ''} still on "
                            + ", ".join(sorted({r['image'] for r in stale})))
        e.update(p)
        e.update({
            "image": image_string(p),
            "digest_source": digest_source,
            "pods": sum(r["pods"] for r in current),
            "running": running,
            "warnings": warnings,
        })
        results.append(e)
    results.sort(key=lambda e: (e["source"] == "", e["source"], e["owner"],
                                e["namespace"], e["kind"], e["name"], e["container"]))
    return results


# --------------------------------------------------------------------------- #
# Git forge links
# --------------------------------------------------------------------------- #
def web_url(url):
    u = (url or "").strip()
    u = re.sub(r"^ssh://(?:[^@/]+@)?([^/:]+)(?::\d+)?/", r"https://\1/", u)
    u = re.sub(r"^[^@/:]+@([^:/]+):", r"https://\1/", u)
    return re.sub(r"\.git/?$", "", u)


def forge_of(base, forges):
    host = urlsplit(base).hostname or ""
    if host in forges:
        return forges[host]
    return next((f for f in ("github", "gitlab", "bitbucket") if f in host), "")


def commit_link(url, sha, forges):
    base = web_url(url)
    f = forge_of(base, forges)
    if not sha or not base.startswith("https://") or not f:
        return code(sha[:12]) if sha else "-"
    path = {"github": "commit", "gitlab": "-/commit", "bitbucket": "commits"}[f]
    return f"[`{sha[:12]}`]({base}/{path}/{sha})"


def compare_link(url, old, new, forges):
    base = web_url(url)
    f = forge_of(base, forges)
    if not (old and new and base.startswith("https://") and f):
        return ""
    return {
        "github": f"[compare]({base}/compare/{old}...{new})",
        "gitlab": f"[compare]({base}/-/compare/{old}...{new})",
        "bitbucket": f"[compare]({base}/branches/compare/{new}%0D{old})",
    }[f]


# --------------------------------------------------------------------------- #
# Diffing
# --------------------------------------------------------------------------- #
def entry_key(e):
    return f"{e['namespace']}/{e['kind']}/{e['name']}/{e['container']}"


def diff_images(previous, current):
    prev = {entry_key(e): e for e in previous}
    curr = {entry_key(e): e for e in current}
    out = []
    for k in sorted(curr.keys() | prev.keys()):
        a, b = prev.get(k), curr.get(k)
        if a and not b:
            out.append(("Removed", a, None))
        elif b and not a:
            out.append(("Added", None, b))
        elif image_string(a, False) != image_string(b, False) or \
                (a["digest"] and b["digest"] and a["digest"] != b["digest"]):
            out.append(("Updated", a, b))
    return out


def diff_dict(prev, curr):
    added = sorted(curr.keys() - prev.keys())
    removed = sorted(prev.keys() - curr.keys())
    common = sorted(curr.keys() & prev.keys())
    return added, removed, common


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def source_heading(skey, sources):
    if not skey:
        return "Not managed by Flux"
    url = sources.get(skey, {}).get("url")
    return pretty(skey) + (f" — {url}" if url else "")


def git_ref_cell(s):
    """'tag v1.2.3' / 'branch main' resolved from the artifact revision."""
    r = s.get("resolved_ref", "")
    if r.startswith("refs/tags/"):
        return f"tag `{r[10:]}`"
    if r.startswith("refs/heads/"):
        return f"branch `{r[11:]}`"
    if "tag" in s["ref"] or "semver" in s["ref"]:
        return f"tag `{r}`" if r else "-"
    return f"`{r}`" if r else "-"


def requested_cell(hr, sources):
    if hr["requested_version"]:
        return code(hr["requested_version"])
    src = sources.get(hr["source"])  # chartRef: the OCIRepository's ref decides the version
    return fmt_ref(src["ref"]) if src else "-"


def chart_source_cell(hr, sources):
    s = sources.get(hr["source"], {})
    label = pretty(hr["source"]) or "-"
    detail = s.get("url", "")
    if hr.get("chart_path"):
        detail += f" path `{hr['chart_path']}`"
    return esc(label) + (f"<br>{esc(detail)}" if detail else "")


def render_changes(snap, prev, forges):
    L = []
    sources, psources = snap["sources"], prev["sources"]

    # Git, OCI and Bucket sources
    rows = []
    added, removed, common = diff_dict(
        {k: v for k, v in psources.items() if v["kind"] != "HelmRepository"},
        {k: v for k, v in sources.items() if v["kind"] != "HelmRepository"})
    for k in common:
        a, b = psources[k], sources[k]
        if (a["url"], a["ref"], a["hash"]) == (b["url"], b["ref"], b["hash"]):
            continue
        what = []
        if a["url"] != b["url"]:
            what.append(f"URL {esc(a['url'])} → {esc(b['url'])}")
        if a["ref"] != b["ref"]:
            what.append(f"ref {fmt_ref(a['ref'])} → {fmt_ref(b['ref'])}")
        frm = f"{code(a['resolved_ref'])} {code(bare_hash(a['hash'])[:12])}"
        to = f"{code(b['resolved_ref'])} {code(bare_hash(b['hash'])[:12])}"
        link = compare_link(b["url"], bare_hash(a["hash"]), bare_hash(b["hash"]), forges) \
            if b["kind"] == "GitRepository" else ""
        rows.append(f"| {esc(pretty(k))} | {esc(b['url'])} | {frm} | {to} | {'; '.join(what + ([link] if link else []))} |")
    for k in added:
        b = sources[k]
        rows.append(f"| {esc(pretty(k))} | {esc(b['url'])} | new | {code(b['resolved_ref'])} "
                    f"{code(bare_hash(b['hash'])[:12])} | |")
    for k in removed:
        a = psources[k]
        rows.append(f"| {esc(pretty(k))} | {esc(a['url'])} | {code(a['resolved_ref'])} "
                    f"{code(bare_hash(a['hash'])[:12])} | removed | |")
    if rows:
        L += ["### Sources", "", "| Source | URL | From | To | Details |", "|---|---|---|---|---|", *rows, ""]

    # Helm charts
    hrs, phrs = snap["helmreleases"], prev["helmreleases"]
    added, removed, common = diff_dict(phrs, hrs)
    rows = []
    for k in common:
        a, b = phrs[k], hrs[k]
        fields = ("chart", "chart_version", "app_version", "chart_digest", "source")
        if any(a.get(f) != b.get(f) for f in fields):
            rows.append(f"| Updated | {esc(pretty(k))} | {code(a['chart'])} {code(a['chart_version'])} "
                        f"(app {code(a['app_version'])}) | {code(b['chart'])} {code(b['chart_version'])} "
                        f"(app {code(b['app_version'])}) |")
    for k in added:
        b = hrs[k]
        rows.append(f"| Added | {esc(pretty(k))} | - | {code(b['chart'])} {code(b['chart_version'])} "
                    f"(app {code(b['app_version'])}) |")
    for k in removed:
        a = phrs[k]
        rows.append(f"| Removed | {esc(pretty(k))} | {code(a['chart'])} {code(a['chart_version'])} | - |")
    if rows:
        L += ["### Helm charts", "", "| Change | HelmRelease | From | To |", "|---|---|---|---|", *rows, ""]

    # Images, grouped by the repo that delivers them
    changes = diff_images(prev["images"], snap["images"])
    if changes:
        L += ["### Images", ""]
        all_sources = {**psources, **sources}
        by_source = defaultdict(list)
        for ch in changes:
            by_source[(ch[2] or ch[1])["source"]].append(ch)
        for skey in sorted(by_source, key=lambda s: (s == "", s)):
            L += [f"#### {esc(source_heading(skey, all_sources))}", "",
                  "| Change | Workload | Container | From | To |", "|---|---|---|---|---|"]
            for change, a, b in by_source[skey]:
                e = b or a
                L.append(f"| {change} | {esc(e['namespace'])}/{e['kind']}/{esc(e['name'])} | "
                         f"{esc(e['container'])} | {code(a['image']) if a else '-'} | "
                         f"{code(b['image']) if b else '-'} |")
            L.append("")
    return L or ["No source, chart or image changes.", ""]


def collect_warnings(snap):
    """[(subject, message)] for everything that needs a human to look at it."""
    sources, ks, hrs = snap["sources"], snap["kustomizations"], snap["helmreleases"]
    out = []
    for k, s in sorted(sources.items()):
        if s["ready"] != "True":
            out.append((pretty(k), f"not ready: {s['message'] or s['ready']}"))
    for k, o in sorted({**ks, **hrs}.items()):
        if o["suspended"]:
            out.append((pretty(k), "suspended"))
        elif o["ready"] != "True":
            out.append((pretty(k), f"not ready: {o['message'] or o['ready']}"))
    for k, o in sorted(ks.items()):
        src = sources.get(o["source"])
        if src and src["revision"] and o["applied_revision"] and src["revision"] != o["applied_revision"]:
            out.append((pretty(k), f"behind its source: applied {short_version(o['applied_revision'])}, "
                                   f"source is at {short_version(src['revision'])}"))
    for e in snap["images"]:
        for w in e["warnings"]:
            ctr = "" if e["container"] == e["name"] else f" ({e['container']})"
            out.append((f"{e['namespace']}/{e['name']}{ctr}", w))
    return out


# --- Summary format -------------------------------------------------------- #
def short_version(revision):
    """'refs/tags/v1.2@sha1:abcdef..' -> 'v1.2 (abcdef1)'; 'main@sha1:..' -> 'main (abcdef1)'."""
    ref, h = parse_revision(revision)
    ref = re.sub(r"^refs/(tags|heads)/", "", ref)
    h = bare_hash(h)[:7]
    if ref and h:
        return f"{ref} ({h})"
    return ref or h or "-"


def repo_label(skey, sources):
    """Human name for a source: the repository path from its URL."""
    s = sources.get(skey)
    if not skey:
        return "Not managed by Flux"
    if not s or not s.get("url"):
        return pretty(skey)
    path = urlsplit(web_url(s["url"])).path.strip("/")
    return path or pretty(skey)


def image_label(e):
    """Short image name for summaries: the last two path segments ('acme/api')."""
    return "/".join(e["repository"].split("/")[-2:])


def tag_or_digest(e):
    if not e:
        return "-"
    return e["tag"] or (e["digest"].replace("sha256:", "")[:7] if e["digest"] else "-")


def render_summary(snap, prev=None, title=None, forges=None):
    forges = forges or {}
    sources, ks, hrs, images = snap["sources"], snap["kustomizations"], snap["helmreleases"], snap["images"]
    lookup = {**ks, **hrs}
    L = render_header(snap, prev, title, "Release notes")

    # ---- What changed, grouped by the repo that delivers it ----
    if prev:
        psources = prev["sources"]
        all_sources = {**psources, **sources}
        groups = defaultdict(lambda: {"source": None, "lines": []})

        for k in sorted(set(sources) | set(psources)):
            a, b = psources.get(k), sources.get(k)
            if (a or b)["kind"] == "HelmRepository":
                continue
            if a and b and (a["url"], a["ref"], a["hash"]) == (b["url"], b["ref"], b["hash"]):
                continue
            if a and b:
                link = compare_link(b["url"], bare_hash(a["hash"]), bare_hash(b["hash"]), forges) \
                    if b["kind"] == "GitRepository" else ""
                groups[k]["source"] = f"{short_version(a['revision'])} → {short_version(b['revision'])}" \
                                      + (f" ({link})" if link else "")
            else:
                groups[k]["source"] = "new source" if b else "source removed"

        phrs = prev["helmreleases"]
        chart_line, nested = {}, defaultdict(list)
        for k in sorted(set(hrs) | set(phrs)):
            a, b = phrs.get(k), hrs.get(k)
            if a and b and all(a.get(f) == b.get(f) for f in ("chart", "chart_version", "chart_digest")):
                continue
            skey = resolve_delivery(lookup if b else {**prev["kustomizations"], **phrs}, k)[0]
            name = (b or a)["chart"]
            if a and b:
                change = f"{a['chart_version']} → {b['chart_version']}"
            else:
                change = f"added, {b['chart_version']}" if b else "removed"
            hr_name = k.split("|")[2]
            label = f"`{esc(name)}`" + ("" if hr_name == name else f" ({esc(hr_name)})")
            groups[skey]["lines"].append(f"Chart {label}: {change}")
            chart_line[k] = (skey, len(groups[skey]["lines"]) - 1)

        for change, a, b in diff_images(prev["images"], images):
            e = b or a
            where = "" if e["repository"].rsplit("/", 1)[-1] == e["name"] else f" in {esc(e['name'])}"
            if change == "Added":
                text = f"`{esc(image_label(b))}:{esc(tag_or_digest(b))}` added{where}"
            elif change == "Removed":
                text = f"`{esc(image_label(a))}` removed{where}"
            elif image_string(a, False) == image_string(b, False):
                text = (f"`{esc(image_label(b))}` {esc(tag_or_digest(b))} re-pushed{where} "
                        f"(digest {a['digest'][7:14]} → {b['digest'][7:14]})")
            elif a["repository"] != b["repository"] or a["registry"] != b["registry"]:
                text = f"`{esc(image_string(a, False))}` → `{esc(image_string(b, False))}`{where}"
            else:
                text = f"`{esc(image_label(b))}` {esc(tag_or_digest(a))} → {esc(tag_or_digest(b))}{where}"
            if e["owner"] in chart_line:  # caused by a chart upgrade: list it under the chart
                nested[chart_line[e["owner"]]].append(text)
            else:
                groups[e["source"]]["lines"].append(text)

        L += ["## What changed", ""]
        if not groups:
            L += ["Nothing changed since the last release.", ""]
        for skey in sorted(groups, key=lambda s: (s == "", repo_label(s, all_sources))):
            g = groups[skey]
            line = f"**{esc(repo_label(skey, all_sources))}**"
            if g["source"]:
                line += f" — {g['source']}"
            L.append(line)
            for i, x in enumerate(g["lines"]):
                L.append(f"- {x}")
                L += [f"  - {y}" for y in nested.get((skey, i), [])]
            L.append("")

    # ---- Needs attention ----
    merged = {}
    for subject, msg in collect_warnings(snap):
        merged.setdefault(subject, []).append(msg)
    warn = [(s, "; ".join(m)) for s, m in merged.items()]
    if warn:
        L += ["## Needs attention", ""]
        L += [f"- **{esc(s)}**: {esc(m)}" for s, m in warn[:10]]
        if len(warn) > 10:
            L.append(f"- …and {len(warn) - 10} more (see the full report)")
        L.append("")

    L += ["## Deployed", ""] + render_deployed(snap, forges, "###")
    return "\n".join(L)


def render_header(snap, prev, title, default_heading):
    heading = title or (f"Release {snap['release']}" if snap.get("release") else default_heading)
    meta = [f"`{snap['context']}`", f"Kubernetes {snap['kubernetes_version']}", f"Flux {snap['flux_version']}",
            snap["generated_at"][:16] + " UTC"]
    if prev:
        meta.append(f"compared with {prev.get('release') or prev['generated_at'][:16]}")
    return [f"# {esc(heading)}", "", " · ".join(meta), ""]


def render_inventory(snap, prev=None, title=None, forges=None):
    """What is deployed right now: repos, Helm charts and images. No comparison."""
    return "\n".join(render_header(snap, None, title, "Deployed versions")
                     + render_deployed(snap, forges or {}, "##"))


def render_deployed(snap, forges, level):
    sources, hrs, images = snap["sources"], snap["helmreleases"], snap["images"]
    L = []
    repos = {k: s for k, s in sources.items() if s["kind"] in ("GitRepository", "OCIRepository", "Bucket")}
    if repos:
        L += [f"{level} Repositories", "", "| Repository | Version | Commit / digest |", "|---|---|---|"]
        for k, s in sorted(repos.items(), key=lambda kv: repo_label(kv[0], sources)):
            ref, h = parse_revision(s["revision"])
            ref = re.sub(r"^refs/(tags|heads)/", "", ref)
            hcell = commit_link(s["url"], bare_hash(h), forges) if s["kind"] == "GitRepository" \
                else code(bare_hash(h)[:12])
            L.append(f"| {esc(repo_label(k, sources))} | {code(ref)} | {hcell} |")
        L.append("")
    if hrs:
        L += [f"{level} Helm charts", "", "| Helm chart | Release | Version |", "|---|---|---|"]
        for k, h in sorted(hrs.items(), key=lambda kv: (kv[1]["chart"], kv[0])):
            L.append(f"| {code(h['chart'])} | {esc(h['target_namespace'] + '/' + h['release_name'])} | "
                     f"{code(h['chart_version'])} |")
        L.append("")
    if images:
        used = defaultdict(set)
        by_ref = {}
        for e in images:
            used[e["image"]].add(e["name"])
            by_ref[e["image"]] = e
        L += [f"{level} Images", "", "| Image | Tag | Digest | Used by |", "|---|---|---|---|"]
        for ref, e in sorted(by_ref.items(), key=lambda kv: (kv[1]["registry"] + "/" + kv[1]["repository"], kv[0])):
            names = sorted(used[ref])
            who = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
            L.append(f"| {code(e['registry'] + '/' + e['repository'])} | {code(e['tag'])} | "
                     f"{code(e['digest']) if e['digest'] else '⚠️ unknown'} | {esc(who)} |")
        L.append("")
    return L


def render_markdown(snap, prev=None, title=None, forges=None):
    forges = forges or {}
    sources, ks, hrs, images = snap["sources"], snap["kustomizations"], snap["helmreleases"], snap["images"]
    git = {k: v for k, v in sources.items() if v["kind"] == "GitRepository"}
    oci = {k: v for k, v in sources.items() if v["kind"] == "OCIRepository"}
    buckets = {k: v for k, v in sources.items() if v["kind"] == "Bucket"}
    helmrepos = {k: v for k, v in sources.items() if v["kind"] == "HelmRepository"}

    L = [f"# {title or ('Release ' + snap['release'] if snap.get('release') else 'Release notes')}", "",
         f"- **Cluster context:** `{snap['context']}`",
         f"- **Kubernetes:** `{snap['kubernetes_version']}`",
         f"- **Flux:** `{snap['flux_version']}`",
         f"- **Generated:** {snap['generated_at']}"]
    if prev:
        L.append(f"- **Compared with:** {prev.get('release') or 'snapshot'} ({prev['generated_at']})")
    L += [f"- **Inventory:** {len(git)} Git repositories, {len(oci)} OCI repositories, "
          f"{len(ks)} Kustomizations, {len(hrs)} HelmReleases, "
          f"{len({e['image'] for e in images})} unique images in {len(images)} containers", ""]

    if prev:
        L += [f"## Changes since {prev.get('release') or 'previous snapshot'}", ""]
        L += render_changes(snap, prev, forges)

    warn = collect_warnings(snap)
    if warn:
        L += ["## Warnings", "", *(f"- **{esc(s)}**: {esc(m)}" for s, m in warn), ""]

    # Sources
    L += ["## Flux sources", ""]
    if git:
        L += ["### Git repositories", "",
              "| GitRepository | URL | Ref (spec) | Resolved | Commit | Ready |", "|---|---|---|---|---|---|"]
        for k, s in sorted(git.items()):
            L.append(f"| {esc(pretty(k))} | {esc(s['url'])} | {fmt_ref(s['ref'])} | {git_ref_cell(s)} | "
                     f"{commit_link(s['url'], bare_hash(s['hash']), forges)}<br>`{bare_hash(s['hash']) or '-'}` | "
                     f"{s['ready']} |")
        L.append("")
    if oci:
        L += ["### OCI repositories", "",
              "| OCIRepository | URL | Ref (spec) | Resolved | Digest | Ready |", "|---|---|---|---|---|---|"]
        for k, s in sorted(oci.items()):
            L.append(f"| {esc(pretty(k))} | {esc(s['url'])} | {fmt_ref(s['ref'])} | {code(s['resolved_ref'])} | "
                     f"{code(s['hash'])} | {s['ready']} |")
        L.append("")
    if buckets:
        L += ["### Buckets", "", "| Bucket | Location | Revision | Ready |", "|---|---|---|---|"]
        for k, s in sorted(buckets.items()):
            L.append(f"| {esc(pretty(k))} | {esc(s['url'])} | {code(s['revision'])} | {s['ready']} |")
        L.append("")
    if helmrepos:
        L += ["### Helm repositories", "", "| HelmRepository | URL | Type | Ready |", "|---|---|---|---|"]
        for k, s in sorted(helmrepos.items()):
            L.append(f"| {esc(pretty(k))} | {esc(s['url'])} | {esc(s['type'] or 'default')} | {s['ready']} |")
        L.append("")

    # Kustomizations
    if ks:
        L += ["## Kustomizations", "",
              "| Kustomization | Source | Path | Applied revision | Ready |", "|---|---|---|---|---|"]
        for k, o in sorted(ks.items()):
            L.append(f"| {esc(pretty(k))} | {esc(pretty(o['source']))} | {code(o['path'])} | "
                     f"{code(o['applied_revision'])} | {'suspended' if o['suspended'] else o['ready']} |")
        L.append("")

    # Helm releases
    if hrs:
        L += ["## Helm releases", "",
              "| HelmRelease | Release | Chart | Version | App version | Requested | Chart source | Chart digest | Ready |",
              "|---|---|---|---|---|---|---|---|---|"]
        for k, h in sorted(hrs.items()):
            L.append(f"| {esc(pretty(k))} | {code(h['target_namespace'] + '/' + h['release_name'])} | "
                     f"{code(h['chart'])} | {code(h['chart_version'])} | {code(h['app_version'])} | "
                     f"{requested_cell(h, sources)} | {chart_source_cell(h, sources)} | "
                     f"{code(h['chart_digest'])} | {'suspended' if h['suspended'] else h['ready']} |")
        L.append("")

    # Images by delivering repo -> Flux owner
    L += ["## Images", ""]
    by_source = defaultdict(lambda: defaultdict(list))
    for e in images:
        by_source[e["source"]][e["owner"]].append(e)
    for skey in sorted(by_source, key=lambda s: (s == "", s)):
        L.append(f"### {esc(source_heading(skey, sources))}")
        s = sources.get(skey)
        if s:
            L.append(f"Ref {fmt_ref(s['ref'])}, resolved {git_ref_cell(s) if s['kind'] == 'GitRepository' else code(s['resolved_ref'])}, "
                     f"{'commit' if s['kind'] == 'GitRepository' else 'revision'} `{bare_hash(s['hash']) or s['revision'] or '-'}`")
        L.append("")
        for owner, items in sorted(by_source[skey].items()):
            L.append(f"#### {esc(pretty(owner)) or 'No Flux owner'}")
            if owner in hrs:
                h = hrs[owner]
                L.append(f"Chart {code(h['chart'])} {code(h['chart_version'])} (app {code(h['app_version'])})  ")
            elif owner in ks:
                L.append(f"Path {code(ks[owner]['path'])}, applied {code(ks[owner]['applied_revision'])}  ")
            if len(items[0]["chain"]) > 1:
                L.append("Applied via: " + " → ".join(esc(pretty(c)) for c in items[0]["chain"]))
            L += ["", "| Workload | Container | Image | Digest from | Pods |", "|---|---|---|---|---|"]
            for e in items:
                wl = f"{esc(e['namespace'])}/{e['kind']}/{esc(e['name'])}"
                if e["controlled_by"]:
                    wl += f"<br>(managed by {esc(e['controlled_by'])})"
                ctr = esc(e["container"]) + ("" if e["container_type"] == "container" else f" ({e['container_type']})")
                L.append(f"| {wl} | {ctr} | {code(e['image'])} | {e['digest_source'] or '⚠️ unknown'} | {e['pods']} |")
            L.append("")

    L += ["## All images", "", "```"] + sorted({e["image"] for e in images}) + ["```", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
FORMATS = {"summary": render_summary, "inventory": render_inventory, "detailed": render_markdown}


def main():
    p = argparse.ArgumentParser(description="Generate release notes from a Flux-managed cluster.")
    p.add_argument("--context", help="kubectl context to use")
    p.add_argument("--kubeconfig", help="path to kubeconfig")
    p.add_argument("-n", "--namespace", help="limit workloads to one namespace (default: all)")
    p.add_argument("--exclude-namespace", action="append", default=None,
                   help=f"namespace to skip; repeatable (default: {', '.join(DEFAULT_EXCLUDED_NAMESPACES)})")
    p.add_argument("--include-unmanaged", action="store_true",
                   help="also list workloads that no Flux object applied")
    p.add_argument("--flux-namespace", default="flux-system", help="namespace Flux is installed in")
    p.add_argument("--resolve-digests", choices=["off", "auto", "crane", "skopeo"], default="off",
                   help="look up digests in the registry for images no pod reports one for")
    p.add_argument("--forge", action="append", default=[], metavar="HOST=TYPE",
                   help="treat a self-hosted Git host as github, gitlab or bitbucket for links; repeatable")
    p.add_argument("--release", default="", help="name of this release (stored in the snapshot)")
    p.add_argument("--previous", help="previous snapshot JSON to diff against")
    p.add_argument("--snapshot-out", help="write this run's snapshot JSON here")
    p.add_argument("--images-out", help="write the unique full image references, one per line, here")
    p.add_argument("--out", help="write Markdown here (default: stdout)")
    p.add_argument("--title", help="heading for the release notes")
    p.add_argument("--format", choices=list(FORMATS), default="summary",
                   help="summary (default): what changed, what needs attention, what is deployed; "
                        "inventory: only what is deployed now, no comparison; "
                        "detailed: every source, Kustomization, HelmRelease and container")
    p.add_argument("--inventory-out", help="also write the inventory here")
    p.add_argument("--detailed-out", help="also write the detailed report here")
    args = p.parse_args()

    forges = {}
    for f in args.forge:
        host, _, kind = f.partition("=")
        if kind not in ("github", "gitlab", "bitbucket"):
            sys.exit(f"--forge {f}: TYPE must be github, gitlab or bitbucket")
        forges[host] = kind

    previous = None
    if args.previous:
        with open(args.previous) as fh:
            previous = json.load(fh)
        if previous.get("schema") != SCHEMA:
            print(f"warning: {args.previous} uses an older snapshot format; skipping the diff", file=sys.stderr)
            previous = None

    kubectl = Kubectl(args.context, args.kubeconfig)
    excluded = set(args.exclude_namespace if args.exclude_namespace is not None
                   else DEFAULT_EXCLUDED_NAMESPACES)
    resolver = DigestResolver(args.resolve_digests) if args.resolve_digests != "off" else None

    flux_ns = kubectl.get_one("namespace", None, args.flux_namespace) or {}
    kustomizations = collect_kustomizations(kubectl)
    helmreleases = collect_helmreleases(kubectl)
    snapshot = {
        "schema": SCHEMA,
        "release": args.release,
        "previous_release": (previous or {}).get("release", ""),
        "context": args.context or kubectl.current_context(),
        "kubernetes_version": kubectl.server_version(),
        "flux_version": (flux_ns.get("metadata", {}).get("labels") or {}).get("app.kubernetes.io/version", "unknown"),
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "sources": collect_sources(kubectl),
        "kustomizations": kustomizations,
        "helmreleases": helmreleases,
        "images": collect_images(kubectl, {**kustomizations, **helmreleases}, args.namespace, excluded,
                                 args.include_unmanaged, resolver),
    }

    if args.snapshot_out:
        with open(args.snapshot_out, "w") as fh:
            json.dump(snapshot, fh, indent=2, sort_keys=True)
    if args.images_out:
        with open(args.images_out, "w") as fh:
            fh.write("\n".join(sorted({e["image"] for e in snapshot["images"]})) + "\n")

    for fmt, path in (("inventory", args.inventory_out), ("detailed", args.detailed_out)):
        if path:
            with open(path, "w") as fh:
                fh.write(FORMATS[fmt](snapshot, previous, args.title, forges) + "\n")
    md = FORMATS[args.format](snapshot, previous, args.title, forges)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(md + "\n")
    else:
        print(md)


if __name__ == "__main__":
    main()
