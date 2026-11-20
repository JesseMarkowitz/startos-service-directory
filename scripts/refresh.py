#!/usr/bin/env python3
"""Refresh the StartOS service directory.

1. Query every registry in registries.toml (JSON-RPC `package.index`) and write a trimmed
   snapshot per registry to data/registry-snapshot/<slug>.json. A registry that fails keeps
   its previous snapshot, so an outage never drops services from the list.
2. Search GitHub for public `*-startos` repositories, keep those pushed in the last 12 months
   and not archived, and classify each: a StartOS 0.4 package has a `startos/` directory at
   its root; a 0.3.x wrapper has a root `manifest.yaml` instead and is left out. Results are
   cached in data/github-cache.json keyed by pushedAt, so unchanged repos cost no API calls.
3. Merge both with the hand-written curated.toml into data/services.json and render README.md.

Standard library only. Needs a GitHub token in GITHUB_TOKEN or GH_TOKEN (falls back to
`gh auth token`). Only public repositories are ever searched (`is:public`).
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SNAP = DATA / "registry-snapshot"
INFO = DATA / "registry-info"
ICONS = DATA / "registry-icons"
UA = "startos-service-directory-refresh"
# Images use absolute URLs: GitHub strips embedded (data:) images, and single-file viewers can't
# resolve relative paths. REGISTRIES.preview.md (gitignored) embeds them for offline viewing.
RAW_BASE = "https://raw.githubusercontent.com/JesseMarkowitz/startos-service-directory/main/"
ACTIVE_DAYS = 365


# ---------------------------------------------------------------- helpers

def http_json(url: str, body: dict | None = None, headers: dict | None = None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def write_json(path: Path, obj) -> bool:
    """Write pretty JSON; return True if the content changed."""
    new = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    old = path.read_text() if path.exists() else None
    if new != old:
        path.write_text(new)
        return True
    return False


def norm_repo(url: str | None) -> str | None:
    if not url:
        return None
    u = url.strip().rstrip("/")
    u = re.sub(r"\.git$", "", u)
    u = re.sub(r"/tree/[^/]+.*$", "", u)
    return u or None


def ver_key(v: str):
    """Rough ExVer ordering: drop a #flavor: prefix, compare all numbers in order."""
    v = re.sub(r"^#[^:]+:", "", v)
    pre = 0 if re.search(r"-(alpha|beta|rc)", v) else 1
    nums = [int(n) for n in re.findall(r"\d+", v.split("-")[0])]
    down = v.rsplit(":", 1)[-1] if ":" in v else "0"
    return (nums, pre, int(re.sub(r"\D", "", down) or 0))


def en(x) -> str | None:
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return x.get("en_US") or x.get("en") or next(iter(x.values()), None)
    return None


# ---------------------------------------------------------------- registries

def fetch_registry(reg: dict) -> tuple[str, dict | None, str | None]:
    url = reg["url"].rstrip("/") + "/rpc/v0"
    try:
        res = http_json(url, {"id": 1, "jsonrpc": "2.0", "method": "package.index", "params": {}})
        if "error" in res:
            return reg["slug"], None, str(res["error"])[:200]
        idx = res["result"]
    except Exception as e:  # noqa: BLE001
        return reg["slug"], None, f"{type(e).__name__}: {e}"[:200]
    cats = {k: v.get("name", k) for k, v in (idx.get("categories") or {}).items()}
    out = {}
    for pid, pkg in idx.get("packages", {}).items():
        versions = pkg.get("versions") or {}
        if not versions:
            continue
        latest = max(versions, key=ver_key)
        m = versions[latest]
        out[pid] = {
            "title": m.get("title") or pid,
            "short": en((m.get("description") or {}).get("short")),
            "license": m.get("license"),
            "packageRepo": norm_repo(m.get("packageRepo")),
            "upstreamRepo": norm_repo(m.get("upstreamRepo")),
            "marketingUrl": m.get("marketingUrl"),
            "version": latest,
            "categories": sorted(cats.get(c, c) for c in (pkg.get("categories") or [])),
        }
    if out:
        out["__categories__"] = sorted(cats.values())
    return reg["slug"], out, None


def fetch_info(reg: dict) -> None:
    """Save the registry's self-description to data/registry-info/<slug>.json and its icon to
    data/registry-icons/. On failure the previous files are kept."""
    import base64
    url = reg["url"].rstrip("/") + "/rpc/v0"
    try:
        r = http_json(url, {"id": 1, "jsonrpc": "2.0", "method": "info", "params": {}})["result"]
    except Exception:  # noqa: BLE001
        return
    INFO.mkdir(parents=True, exist_ok=True)
    ICONS.mkdir(parents=True, exist_ok=True)
    icon_file = None
    m = re.match(r"data:image/(png|jpeg|jpg|svg\+xml|webp|gif);base64,(.+)", r.get("icon") or "", re.S)
    if m:
        ext = {"svg+xml": "svg", "jpeg": "jpg"}.get(m.group(1), m.group(1))
        for old in ICONS.glob(f"{reg['slug']}.*"):
            old.unlink()
        icon_file = f"{reg['slug']}.{ext}"
        (ICONS / icon_file).write_bytes(base64.b64decode(m.group(2)))
    desc = en(r.get("description"))
    write_json(INFO / f"{reg['slug']}.json", {
        "name": r.get("name"),
        "description": desc.strip() if desc and desc.strip() else None,
        "icon": icon_file,
        "signers": sorted({s.get("name") for s in (r.get("trustSigners") or {}).values() if s.get("name")}),
    })


def refresh_registries(registries: list[dict]) -> dict:
    with cf.ThreadPoolExecutor(8) as ex:
        list(ex.map(fetch_info, registries))
    SNAP.mkdir(parents=True, exist_ok=True)
    status = {}
    with cf.ThreadPoolExecutor(8) as ex:
        for slug, snap, err in ex.map(fetch_registry, registries):
            path = SNAP / f"{slug}.json"
            if snap is not None:
                write_json(path, snap)
                status[slug] = {"ok": True, "packages": len(snap) - ("__categories__" in snap)}
            else:
                kept = len([k for k in json.loads(path.read_text()) if k != "__categories__"]) if path.exists() else 0
                status[slug] = {"ok": False, "error": err, "keptPackages": kept}
            print(f"registry {slug}: {status[slug]}", file=sys.stderr)
    return status


# ---------------------------------------------------------------- github

def gh_token() -> str:
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t:
        return t
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        sys.exit("No GitHub token: set GITHUB_TOKEN or log in with `gh auth login`.")


class GH:
    def __init__(self, token: str):
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                  "X-GitHub-Api-Version": "2022-11-28"}

    def get(self, path: str, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return http_json(f"https://api.github.com{path}{q}", headers=self.h)

    def raw(self, repo: str, path: str) -> str | None:
        try:
            r = self.get(f"/repos/{repo}/contents/{path}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        if isinstance(r, dict) and r.get("encoding") == "base64":
            import base64
            return base64.b64decode(r["content"]).decode("utf-8", "replace")
        return None


def search_startos_repos(gh: GH) -> list[dict]:
    repos, page = [], 1
    while True:
        r = gh.get("/search/repositories", q="startos in:name is:public", per_page=100, page=page)
        items = r.get("items", [])
        repos += items
        if len(items) < 100 or page >= 10:
            if r.get("total_count", 0) > 1000:
                print(f"WARNING: {r['total_count']} results; GitHub search caps at 1000", file=sys.stderr)
            break
        page += 1
    seen, out = set(), []
    for it in repos:
        fn = it["full_name"]
        if fn in seen or "-startos" not in it["name"].lower() or it.get("private"):
            continue
        seen.add(fn)
        out.append({"fullName": fn, "pushedAt": it["pushed_at"], "archived": it["archived"],
                    "fork": it["fork"], "description": it.get("description"),
                    "url": it["html_url"]})
    return out


MANIFEST_PATHS = ["startos/manifest/index.ts", "startos/manifest.ts"]


def ts_field(src: str, field: str) -> str | None:
    m = re.search(rf"\b{field}\s*:\s*(['\"`])(.+?)\1", src)
    return m.group(2) if m else None


def classify(gh: GH, repo: dict) -> dict:
    fn = repo["fullName"]
    try:
        root = gh.get(f"/repos/{fn}/contents")
    except urllib.error.HTTPError as e:
        if e.code == 404:  # empty repository
            return {"pushedAt": repo["pushedAt"], "kind": "empty"}
        return {"pushedAt": repo["pushedAt"], "kind": "error", "error": f"HTTP {e.code}"}
    names = {x["name"] for x in root} if isinstance(root, list) else set()
    if "startos" in names:
        kind = "v04"
    elif "manifest.yaml" in names or "manifest.yml" in names:
        kind = "v03"
    else:
        kind = "other"
    info = {"pushedAt": repo["pushedAt"], "kind": kind}
    if kind == "v04":
        for p in MANIFEST_PATHS:
            src = gh.raw(fn, p)
            if src:
                info["manifest"] = {
                    "id": ts_field(src, "id"),
                    "title": ts_field(src, "title"),
                    "license": ts_field(src, "license"),
                    "upstreamRepo": norm_repo(ts_field(src, "upstreamRepo")),
                    "packageRepo": norm_repo(ts_field(src, "packageRepo")),
                }
                break
    return info


def refresh_github() -> tuple[list[dict], dict]:
    gh = GH(gh_token())
    cache_path = DATA / "github-cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    repos = search_startos_repos(gh)
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=ACTIVE_DAYS)).isoformat()
    active = [r for r in repos if not r["archived"] and not r["fork"] and r["pushedAt"] >= cutoff]
    todo = [r for r in active if cache.get(r["fullName"], {}).get("pushedAt") != r["pushedAt"]
            or cache[r["fullName"]].get("kind") == "error"]
    print(f"github: {len(repos)} *-startos repos, {len(active)} active, {len(todo)} to (re)classify",
          file=sys.stderr)
    with cf.ThreadPoolExecutor(6) as ex:
        for r, info in zip(todo, ex.map(lambda r: classify(gh, r), todo)):
            cache[r["fullName"]] = info
    live = {r["fullName"] for r in active}
    cache = {k: v for k, v in cache.items() if k in live}
    write_json(cache_path, cache)
    return active, cache


# ---------------------------------------------------------------- merge + render

def gh_path(url: str | None) -> str | None:
    if url and "github.com/" in url:
        return url.split("github.com/", 1)[1].lower()
    return None


def build(registries: list[dict], status: dict, active: list[dict], cache: dict, curated: dict) -> dict:
    services: dict[str, dict] = {}
    for reg in registries:
        path = SNAP / f"{reg['slug']}.json"
        if not path.exists():
            continue
        for pid, m in json.loads(path.read_text()).items():
            if pid == "__categories__":
                continue
            s = services.setdefault(pid, {"id": pid, "title": m["title"], "short": m["short"],
                                          "license": m["license"], "categories": [],
                                          "registries": {}, "packageRepos": [], "upstreamRepo": None,
                                          "otherRepos": []})
            s["registries"][reg["slug"]] = m["version"]
            for c in m["categories"]:
                if c not in s["categories"]:
                    s["categories"].append(c)
            if m["packageRepo"] and m["packageRepo"] not in s["packageRepos"]:
                s["packageRepos"].append(m["packageRepo"])
            s["upstreamRepo"] = s["upstreamRepo"] or m["upstreamRepo"]
            s["short"] = s["short"] or m["short"]
            s["license"] = s["license"] or m["license"]

    registered = {gh_path(u) for s in services.values() for u in s["packageRepos"]}
    by_repo = {r["fullName"]: r for r in active}
    for fn, info in sorted(cache.items()):
        if info.get("kind") != "v04" or fn.lower() in registered or fn not in by_repo:
            continue
        repo = by_repo[fn]
        man = info.get("manifest") or {}
        repo_id = fn.split("/")[1].lower().removesuffix("-startos")
        pid = man.get("id") or repo_id
        if pid == "hello-world" and repo_id != "hello-world":
            pid = repo_id  # template id never changed; the repo name is the better guess
        if pid in services and services[pid]["registries"]:
            services[pid]["otherRepos"].append(repo["url"])
            continue
        s = services.setdefault(pid, {"id": pid, "title": man.get("title") or pid,
                                      "short": repo["description"], "license": man.get("license"),
                                      "categories": [], "registries": {}, "packageRepos": [],
                                      "upstreamRepo": None, "otherRepos": [], "lastPush": None})
        s["packageRepos"].append(repo["url"])
        s["upstreamRepo"] = s["upstreamRepo"] or man.get("upstreamRepo")
        s["lastPush"] = max(filter(None, [s.get("lastPush"), repo["pushedAt"][:10]]))

    for pid, c in curated.items():
        if pid not in services:
            continue
        s = services[pid]
        for k in ("replaces", "note", "exclude"):
            if k in c:
                s[k] = c[k]
        if c.get("upstream"):
            s["upstreamRepo"] = c["upstream"]
    for s in services.values():
        s["packageRepos"].sort()
        s["otherRepos"].sort()
    return {"registries": status, "services": dict(sorted(services.items()))}


def link(url: str | None) -> str:
    if not url:
        return "—"
    label = url.split("github.com/", 1)[1] if "github.com/" in url else re.sub(r"^https?://", "", url)
    return f"[{label}]({url})"


def cell(text: str | None) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def render(result: dict, registries: list[dict], as_of: str) -> str:
    head = (ROOT / "templates" / "README.head.md").read_text()
    names = {r["slug"]: r["name"] for r in registries}
    services = [s for s in result["services"].values() if not s.get("exclude")]
    on_reg = [s for s in services if s["registries"]]
    gh_only = [s for s in services if not s["registries"]]

    def row(s, registered: bool):
        title = f"**{cell(s['title'])}**" + (f"<br>{cell(s['short'])}" if s.get("short") else "")
        if registered:
            where = "<br>".join(names[k] for k in names if k in s["registries"])
        else:
            where = f"GitHub only<br>last push {s.get('lastPush') or '?'}"
        pkg = "<br>".join(link(u) for u in s["packageRepos"]) or "—"
        rep = cell(s.get("replaces")) or ""
        if s.get("note"):
            rep += ("<br>" if rep else "") + f"_{cell(s['note'])}_"
        return f"| {title} | {where} | {pkg} | {link(s['upstreamRepo'])} | {rep or '—'} |"

    hdr = ("| Service | Found on | StartOS package repo | Upstream project | Open-source alternative to |\n"
           "|---|---|---|---|---|")
    reg_rows = []
    for r in registries:
        st = result["registries"].get(r["slug"], {})
        if st.get("ok"):
            state = f"{st['packages']} packages"
        else:
            state = f"unreachable at last check ({st.get('keptPackages', 0)} kept from earlier)"
        reg_rows.append(f"| {r['name']} | `{r['url']}` | {r['operator']} | {state} |")

    parts = [
        head.rstrip(),
        "",
        f"_List generated {as_of}: {len(on_reg)} services on registries, "
        f"{len(gh_only)} more on GitHub only._",
        "",
        "## Registries checked",
        "",
        "Full list with notes: [REGISTRIES.md](REGISTRIES.md). Registry operators can add theirs by pull request.",
        "",
        "| Registry | Address to add in StartOS | Run by | Status |",
        "|---|---|---|---|",
        *reg_rows,
        "",
        "## Services on a registry",
        "",
        hdr,
        *[row(s, True) for s in sorted(on_reg, key=lambda s: s["title"].lower())],
        "",
        "## Services on GitHub only",
        "",
        "Public `*-startos` repositories that target StartOS 0.4, were pushed to in the last 12 months, "
        "and are not on any registry above. You would have to build and sideload these yourself. "
        "Older 0.3.x wrappers are left out.",
        "",
        hdr,
        *[row(s, False) for s in sorted(gh_only, key=lambda s: s["title"].lower())],
        "",
    ]
    return "\n".join(parts)


def render_registries(result: dict, registries: list[dict], as_of: str, embed: bool = False) -> str:
    import base64
    rows = []
    for r in registries:
        st = result["registries"].get(r["slug"], {})
        ip = INFO / f"{r['slug']}.json"
        info = json.loads(ip.read_text()) if ip.exists() else {}
        sp = SNAP / f"{r['slug']}.json"
        snap = json.loads(sp.read_text()) if sp.exists() else {}
        cat_names = snap.get("__categories__", [])
        counts = {c: 0 for c in cat_names}
        for k, m in snap.items():
            if k != "__categories__":
                for c in m["categories"]:
                    counts[c] = counts.get(c, 0) + 1
        n = len([k for k in snap if k != "__categories__"])
        icon = ""
        if info.get("icon"):
            ext = info["icon"].rsplit(".", 1)[1]
            if embed:
                mime = {"svg": "svg+xml", "jpg": "jpeg"}.get(ext, ext)
                src = f"data:image/{mime};base64," + base64.b64encode((ICONS / info["icon"]).read_bytes()).decode()
            else:
                src = f"{RAW_BASE}data/registry-icons/{info['icon']}"
            icon = f'<img src="{src}" alt="{cell(r["name"])} icon" width="40" height="40">'
        if info.get("description"):
            desc = cell(info["description"].replace("\n\n", "<br>"))
        elif info:
            desc = "_No description provided by the registry owner._"
        else:
            desc = "_Did not answer; description unknown._"
        if r.get("note"):
            desc += f"<br>_Note: {cell(r['note'])}_"
        name = f"**{cell(r['name'])}**<br>[{r['url']}]({r['url']})<br>{desc}"
        signers = ", ".join(info.get("signers") or []) or "—"
        nb = lambda x: cell(x).replace(" ", "&nbsp;")  # wrap only between categories
        cats = ", ".join(nb(f"{c} ({counts[c]})") for c in sorted(cat_names)) or "—"
        status = "✅" if st.get("ok") else "⚠️"
        rows.append(f"| {icon} | {name} | {cell(r['operator'])} | {cell(signers)} | {n or '—'} | {cats} | {status} |")
    return "\n".join([
        "# StartOS registries",
        "",
        "Every StartOS registry this project knows of. Generated from [`registries.toml`](registries.toml) and "
        "from what each registry says about itself; to add yours, see [CONTRIBUTING.md](CONTRIBUTING.md).",
        "",
        "> [!WARNING]",
        "> Being listed here means only that the registry exists and answered. **No registry is vetted or "
        "endorsed.** Adding a registry to StartOS trusts its signing keys for packages. "
        "See the [disclaimer](DISCLAIMER.md).",
        "",
        f"_Checked {as_of}. St (status): ✅ answered · ⚠️ unreachable at last check (last known data kept). "
        "Svcs: services installable from that registry. Categories: services in each, as the registry assigns them._",
        "",
        "| | Registry | Run by | Trusted signers | Svcs | Categories | St |",
        "|---|---|---|---|---|---|---|",
        *rows,
        "",
    ])


def main():
    registries = tomllib.loads((ROOT / "registries.toml").read_text())["registry"]
    curated_path = ROOT / "curated.toml"
    curated = tomllib.loads(curated_path.read_text()) if curated_path.exists() else {}
    offline = "--offline" in sys.argv  # re-render from existing data only
    if offline:
        prev = json.loads((DATA / "services.json").read_text())
        status = prev["registries"]
        cache = json.loads((DATA / "github-cache.json").read_text())
        active = [{"fullName": k, "pushedAt": v["pushedAt"], "description": None,
                   "url": f"https://github.com/{k}"} for k, v in cache.items()]
        # keep descriptions from the previous build
        desc = {u: s.get("short") for s in prev["services"].values() for u in s["packageRepos"]}
        for a in active:
            a["description"] = desc.get(a["url"])
    else:
        status = refresh_registries(registries)
        active, cache = refresh_github()
    result = build(registries, status, active, cache, curated)
    as_of_path = DATA / "as-of.txt"
    data_changed = write_json(DATA / "services.json", result)
    if data_changed or not as_of_path.exists():
        as_of_path.write_text(dt.date.today().isoformat() + "\n")
    readme = render(result, registries, as_of_path.read_text().strip())
    (ROOT / "README.md").write_text(readme)
    as_of = as_of_path.read_text().strip()
    (ROOT / "REGISTRIES.md").write_text(render_registries(result, registries, as_of))
    (ROOT / "REGISTRIES.preview.md").write_text(render_registries(result, registries, as_of, embed=True))
    missing = [s["id"] for s in result["services"].values()
               if not s.get("exclude") and "replaces" not in s]
    print(f"services: {len(result['services'])}; without a curated 'replaces': {len(missing)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
