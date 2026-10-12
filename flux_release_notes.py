#!/usr/bin/env python3
"""
Generate release notes from the container images running in a Kubernetes
cluster whose workloads are managed by FluxCD.

Designed for multi-repo Flux setups (a root repo that syncs in other teams'
repos). It never reads any Git repo: everything is derived from the cluster,
so it works regardless of how each team lays out its repository.

How ownership is resolved
  workload --labels--> HelmRelease or Kustomization that applied it
  HelmRelease --labels--> Kustomization that applied the HelmRelease
  Kustomization --spec.sourceRef--> GitRepository / OCIRepository / Bucket
  Kustomization --labels--> parent Kustomization ... up to the root

The nearest Kustomization's source is treated as the "delivering repo" (i.e.
the team repo), and the full chain is shown so you can see how it got there.

Output
  * Markdown release notes grouped by source repo, then by Flux object.
  * A JSON snapshot. Pass a previous one with --previous to get a "what changed"
    section: source revision changes (with GitHub/GitLab compare links),
    image updates/additions/removals per repo, and Helm chart version changes.

Requirements: Python 3.8+, kubectl on PATH with read access to the cluster.

Examples
  ./flux_release_notes.py --snapshot-out snapshots/r1.json --out RELEASE_NOTES.md
  ./flux_release_notes.py --previous snapshots/r1.json \
      --snapshot-out snapshots/r2.json --out RELEASE_NOTES.md --title "Release 2026.10.2"
"""

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections import defaultdict

WORKLOAD_KINDS = "deployments,statefulsets,daemonsets,cronjobs"
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
        return json.loads(out)["items"] if out else []

    def current_context(self):
        out = self.run(["config", "current-context"], allow_fail=True)
        return out.strip() if out else "unknown"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def key(kind, ns, name):
    return f"{kind}|{ns}|{name}" if kind else ""


def pretty(k):
    if not k:
        return ""
    kind, ns, name = k.split("|")
    return f"{kind} {ns}/{name}"


def split_image(image):
    """'reg.io/team/app:1.2.3@sha256:abc' -> ('reg.io/team/app', '1.2.3', 'sha256:abc')"""
    digest = ""
    if "@" in image:
        image, digest = image.split("@", 1)
    repo, tag = image, ""
    if ":" in image.rsplit("/", 1)[-1]:
        repo, tag = image.rsplit(":", 1)
    return repo, tag or ("" if digest else "latest"), digest


def digest_from_image_id(image_id):
    return image_id.split("@", 1)[1] if "@" in (image_id or "") else ""


def pod_spec(obj):
    spec = obj.get("spec", {})
    if obj["kind"] == "CronJob":
        return spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
    return spec.get("template", {}).get("spec", {})


def flux_owner(labels):
    labels = labels or {}
    if labels.get(HR_NAME):
        return key("HelmRelease", labels.get(HR_NS, ""), labels[HR_NAME])
    if labels.get(KS_NAME):
        return key("Kustomization", labels.get(KS_NS, ""), labels[KS_NAME])
    return ""


def ready_state(status):
    for c in status.get("conditions", []):
        if c.get("type") == "Ready":
            return c.get("status", "Unknown")
    return "Unknown"


def ref_key(ref, default_ns):
    ref = ref or {}
    return key(ref.get("kind", ""), ref.get("namespace") or default_ns, ref.get("name", ""))


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
def collect_sources(kubectl):
    sources = {}
    for kind, resource in SOURCE_RESOURCES.items():
        for s in kubectl.get_json(resource, allow_fail=True):
            md, spec, st = s["metadata"], s.get("spec", {}), s.get("status", {})
            ref = spec.get("ref", {}) or {}
            ref_s = next((f"{k} {ref[k]}" for k in ("tag", "semver", "branch", "name", "commit", "digest")
                          if ref.get(k)), "")
            sources[key(kind, md["namespace"], md["name"])] = {
                "url": spec.get("url") or spec.get("bucketName", ""),
                "ref": ref_s,
                "revision": (st.get("artifact") or {}).get("revision", ""),
                "ready": ready_state(st),
            }
    return sources


def collect_flux_objects(kubectl):
    flux = {}
    for ks in kubectl.get_json("kustomizations.kustomize.toolkit.fluxcd.io", allow_fail=True):
        md, spec, st = ks["metadata"], ks.get("spec", {}), ks.get("status", {})
        flux[key("Kustomization", md["namespace"], md["name"])] = {
            "parent": flux_owner(md.get("labels")),
            "source": ref_key(spec.get("sourceRef"), md["namespace"]),
            "path": spec.get("path", ""),
            "revision": st.get("lastAppliedRevision", ""),
            "ready": ready_state(st),
        }
    for hr in kubectl.get_json("helmreleases.helm.toolkit.fluxcd.io", allow_fail=True):
        md, spec, st = hr["metadata"], hr.get("spec", {}), hr.get("status", {})
        chart_spec = (spec.get("chart") or {}).get("spec", {})
        chart_src = ref_key(chart_spec.get("sourceRef"), md["namespace"]) if chart_spec \
            else ref_key(spec.get("chartRef"), md["namespace"])
        history = st.get("history") or []
        if history:  # helm.toolkit.fluxcd.io/v2
            chart = history[0].get("chartName", chart_spec.get("chart", ""))
            version = history[0].get("chartVersion", "")
            app_version = history[0].get("appVersion", "")
        else:        # v2beta1/v2beta2
            chart = chart_spec.get("chart", "")
            version = st.get("lastAppliedRevision") or st.get("lastAttemptedRevision", "")
            app_version = ""
        flux[key("HelmRelease", md["namespace"], md["name"])] = {
            "parent": flux_owner(md.get("labels")),
            "source": chart_src,
            "chart": chart,
            "chart_version": version,
            "app_version": app_version,
            "ready": ready_state(st),
        }
    return flux


def resolve(flux, owner):
    """Return (delivering source key, chain of Flux keys from root to owner)."""
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
    if not delivering and owner in flux:  # HelmRelease applied outside a Kustomization
        delivering = flux[owner]["source"]
    return delivering, list(reversed(chain))


def collect_running_digests(kubectl, namespace):
    digests = defaultdict(set)
    for pod in kubectl.get_json("pods", namespace):
        ns = pod["metadata"]["namespace"]
        st = pod.get("status", {})
        for cs in st.get("containerStatuses", []) + st.get("initContainerStatuses", []):
            d = digest_from_image_id(cs.get("imageID", ""))
            if d:
                digests[(ns, cs.get("image", ""))].add(d)
    return digests


def collect_images(kubectl, flux, namespace, excluded, include_unmanaged):
    digests = collect_running_digests(kubectl, namespace)
    entries = []
    for obj in kubectl.get_json(WORKLOAD_KINDS, namespace):
        md = obj["metadata"]
        ns = md["namespace"]
        if ns in excluded:
            continue
        owner = flux_owner(md.get("labels"))
        if not owner and not include_unmanaged:
            continue
        source, chain = resolve(flux, owner) if owner else ("", [])
        spec = pod_spec(obj)
        containers = [(c, False) for c in spec.get("containers", [])]
        containers += [(c, True) for c in spec.get("initContainers", [])]
        for c, is_init in containers:
            image = c.get("image", "")
            repo, tag, pinned = split_image(image)
            running = sorted(digests.get((ns, image), set()))
            entries.append({
                "namespace": ns, "kind": obj["kind"], "name": md["name"],
                "container": c.get("name", ""), "init": is_init,
                "image": image, "repository": repo, "tag": tag,
                "digest": pinned or (running[0] if len(running) == 1 else ""),
                "digest_mismatch": len(running) > 1,
                "owner": owner, "source": source, "chain": chain,
            })
    entries.sort(key=lambda e: (e["source"] == "", e["source"], e["owner"],
                                e["namespace"], e["kind"], e["name"], e["container"]))
    return entries


# --------------------------------------------------------------------------- #
# Diffing
# --------------------------------------------------------------------------- #
def entry_key(e):
    return f"{e['namespace']}/{e['kind']}/{e['name']}/{e['container']}"


def diff_images(previous, current):
    """Returns list of (change, before, after), change in Updated/Added/Removed."""
    prev = {entry_key(e): e for e in previous}
    curr = {entry_key(e): e for e in current}
    out = []
    for k in sorted(curr.keys() | prev.keys()):
        a, b = prev.get(k), curr.get(k)
        if a and not b:
            out.append(("Removed", a, None))
        elif b and not a:
            out.append(("Added", None, b))
        elif a["image"] != b["image"] or (a["digest"] and b["digest"] and a["digest"] != b["digest"]):
            out.append(("Updated", a, b))
    return out


def git_sha(revision):
    m = re.search(r"sha1:([0-9a-f]{7,40})$", revision or "") or re.search(r"/([0-9a-f]{40})$", revision or "")
    return m.group(1) if m else ""


def web_url(url):
    u = (url or "").strip()
    u = re.sub(r"^ssh://(?:[^@]+@)?([^/:]+)(?::\d+)?/", r"https://\1/", u)
    u = re.sub(r"^[^@/]+@([^:/]+):", r"https://\1/", u)
    return re.sub(r"\.git/?$", "", u)


def compare_link(url, old_rev, new_rev):
    old, new, base = git_sha(old_rev), git_sha(new_rev), web_url(url)
    if not (old and new and base.startswith("https://")):
        return ""
    if "github" in base:
        return f"[compare]({base}/compare/{old}...{new})"
    if "gitlab" in base:
        return f"[compare]({base}/-/compare/{old}...{new})"
    if "bitbucket" in base:
        return f"[compare]({base}/branches/compare/{new}%0D{old})"
    return ""


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def short_rev(rev):
    if not rev:
        return "-"
    m = re.match(r"^(.*?)@?(sha1|sha256):([0-9a-f]+)$", rev)
    return f"{m.group(1)}@{m.group(3)[:12]}" if m else rev


def fmt_version(e):
    if not e:
        return "-"
    d = e["digest"].replace("sha256:", "")[:12] if e["digest"] else ""
    return f"`{e['tag'] or '-'}`" + (f" ({d})" if d else "")


def source_heading(skey, sources):
    if not skey:
        return "Not managed by Flux"
    s = sources.get(skey, {})
    return f"{pretty(skey)}" + (f" — {web_url(s['url'])}" if s.get("url") else "")


def render_markdown(snap, prev=None, title=None):
    entries, flux, sources = snap["images"], snap["flux"], snap["sources"]
    L = [f"# {title or 'Release notes'}", "",
         f"- **Cluster context:** `{snap['context']}`",
         f"- **Generated:** {snap['generated_at']}",
         f"- **Source repos delivering workloads:** {len({e['source'] for e in entries if e['source']})}",
         f"- **Workload containers:** {len(entries)} ({len({e['image'] for e in entries})} unique images)"]
    if prev:
        L.append(f"- **Compared with snapshot from:** {prev['generated_at']}")
    L.append("")

    if prev:
        L += ["## Changes since previous release", ""]
        any_change = False

        # Source revisions (one row per team repo whose commit changed)
        prev_sources = prev.get("sources", {})
        rows = []
        for skey, s in sorted(sources.items()):
            p = prev_sources.get(skey)
            if p and p.get("revision") != s.get("revision"):
                rows.append(f"| {pretty(skey)} | {web_url(s['url'])} | `{short_rev(p.get('revision'))}` | "
                            f"`{short_rev(s.get('revision'))}` | {compare_link(s['url'], p.get('revision'), s.get('revision'))} |")
        for skey in sorted(sources.keys() - prev_sources.keys()):
            rows.append(f"| {pretty(skey)} | {web_url(sources[skey]['url'])} | new | "
                        f"`{short_rev(sources[skey].get('revision'))}` | |")
        for skey in sorted(prev_sources.keys() - sources.keys()):
            rows.append(f"| {pretty(skey)} | {web_url(prev_sources[skey].get('url'))} | "
                        f"`{short_rev(prev_sources[skey].get('revision'))}` | removed | |")
        if rows:
            any_change = True
            L += ["### Source revisions", "", "| Source | URL | From | To | Diff |", "|---|---|---|---|---|", *rows, ""]

        # Image changes grouped by delivering repo
        changes = diff_images(prev["images"], entries)
        if changes:
            any_change = True
            L += ["### Image changes", ""]
            by_source = defaultdict(list)
            for ch in changes:
                by_source[(ch[2] or ch[1]).get("source", "")].append(ch)
            for skey in sorted(by_source, key=lambda s: (s == "", s)):
                L += [f"#### {source_heading(skey, sources or prev_sources)}", "",
                      "| Change | Workload | Container | Image | From | To |", "|---|---|---|---|---|---|"]
                for change, a, b in by_source[skey]:
                    e = b or a
                    L.append(f"| {change} | {e['namespace']}/{e['kind']}/{e['name']} | {e['container']} | "
                             f"`{e['repository']}` | {fmt_version(a)} | {fmt_version(b)} |")
                L.append("")

        # Helm chart version changes
        prev_flux = prev.get("flux", {})
        chart_rows = [f"| {pretty(k)} | `{v.get('chart', '')}` | `{prev_flux[k].get('chart_version', '')}` | `{v['chart_version']}` |"
                      for k, v in sorted(flux.items())
                      if k.startswith("HelmRelease|") and k in prev_flux
                      and prev_flux[k].get("chart_version") != v.get("chart_version")]
        if chart_rows:
            any_change = True
            L += ["### Helm chart versions", "", "| HelmRelease | Chart | From | To |", "|---|---|---|---|", *chart_rows, ""]

        if not any_change:
            L += ["No source, image or chart changes.", ""]

    # Full inventory: source repo -> Flux object -> containers
    L += ["## Deployed components", ""]
    by_source = defaultdict(lambda: defaultdict(list))
    for e in entries:
        by_source[e["source"]][e["owner"]].append(e)

    for skey in sorted(by_source, key=lambda s: (s == "", s)):
        L.append(f"### {source_heading(skey, sources)}")
        s = sources.get(skey)
        if s:
            L.append(f"Revision `{short_rev(s['revision'])}`" + (f" ({s['ref']})" if s["ref"] else "")
                     + f", Ready: {s['ready']}")
        L.append("")
        for owner, items in sorted(by_source[skey].items()):
            obj = flux.get(owner, {})
            L.append(f"#### {pretty(owner) or 'No Flux owner'}")
            if owner.startswith("HelmRelease|") and obj:
                L.append(f"Chart `{obj['chart']}` `{obj['chart_version']}`"
                         + (f", app `{obj['app_version']}`" if obj.get("app_version") else "")
                         + f" from {pretty(obj['source'])}, Ready: {obj['ready']}  ")
            elif obj:
                L.append(f"Path `{obj['path'] or '.'}`, Ready: {obj['ready']}  ")
            chain = items[0]["chain"]
            if len(chain) > 1:
                L.append("Applied via: " + " → ".join(pretty(c) for c in chain))
            L += ["", "| Workload | Container | Image | Version |", "|---|---|---|---|"]
            for e in items:
                warn = " ⚠️ multiple digests running" if e["digest_mismatch"] else ""
                L.append(f"| {e['namespace']}/{e['kind']}/{e['name']} | {e['container']}"
                         f"{' (init)' if e['init'] else ''} | `{e['repository']}` | {fmt_version(e)}{warn} |")
            L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="Generate release notes from images in a Flux-managed cluster.")
    p.add_argument("--context", help="kubectl context to use")
    p.add_argument("--kubeconfig", help="path to kubeconfig")
    p.add_argument("-n", "--namespace", help="limit workloads to one namespace (default: all)")
    p.add_argument("--exclude-namespace", action="append", default=None,
                   help=f"namespace to skip; repeatable (default: {', '.join(DEFAULT_EXCLUDED_NAMESPACES)})")
    p.add_argument("--include-unmanaged", action="store_true",
                   help="also list workloads that have no Flux labels")
    p.add_argument("--previous", help="previous snapshot JSON to diff against")
    p.add_argument("--snapshot-out", help="write the current snapshot JSON here")
    p.add_argument("--out", help="write Markdown here (default: stdout)")
    p.add_argument("--title", help="heading for the release notes")
    args = p.parse_args()

    kubectl = Kubectl(args.context, args.kubeconfig)
    excluded = set(args.exclude_namespace if args.exclude_namespace is not None
                   else DEFAULT_EXCLUDED_NAMESPACES)

    flux = collect_flux_objects(kubectl)
    snapshot = {
        "context": args.context or kubectl.current_context(),
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "sources": collect_sources(kubectl),
        "flux": flux,
        "images": collect_images(kubectl, flux, args.namespace, excluded, args.include_unmanaged),
    }

    previous = None
    if args.previous:
        with open(args.previous) as fh:
            previous = json.load(fh)

    if args.snapshot_out:
        with open(args.snapshot_out, "w") as fh:
            json.dump(snapshot, fh, indent=2)

    md = render_markdown(snapshot, previous, args.title)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(md + "\n")
    else:
        print(md)


if __name__ == "__main__":
    main()
