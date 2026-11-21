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
# resolve relative paths. README.preview.md (gitignored) embeds them for offline viewing.
REPO_URL = "https://github.com/JesseMarkowitz/startos-service-directory"
SITE_URL = "https://jessemarkowitz.github.io/startos-service-directory/"
RAW_BASE = "https://raw.githubusercontent.com/JesseMarkowitz/startos-service-directory/main/"
FORGE_URLS = {"GitHub": "https://github.com", "GitLab": "https://gitlab.com", "Codeberg": "https://codeberg.org"}
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
            "published": max((x[1].get("publishedAt") or "")[:10] for v in versions.values()
                             for x in (v.get("s9pks") or [[None, {}]])) or None,
            "categories": sorted(cats.get(c, c) for c in (pkg.get("categories") or [])),
        }
    if out:
        out["__categories__"] = sorted(cats.values())
    return reg["slug"], out, None


def back_light_icon(path: Path) -> None:
    """Icons drawn for StartOS's dark UI (light strokes on transparency) vanish on a white page.
    Put those, and only those, on a dark rounded square. Needs Pillow; skipped without it."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    if path.suffix.lower() not in (".png", ".webp", ".gif", ".jpg"):
        return
    im = Image.open(path).convert("RGBA")
    px = [p for p in getattr(im, "get_flattened_data", im.getdata)() if p[3] > 20]
    if not px or len(px) / (im.width * im.height) > 0.7:
        return  # mostly opaque: it brings its own background
    lum = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b, _ in px) / len(px)
    if lum < 180:
        return
    w, h = im.size
    bg = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(bg).rounded_rectangle([0, 0, w - 1, h - 1], radius=w // 6, fill=(36, 41, 47, 255))
    bg.alpha_composite(im)
    bg.save(path.with_suffix(".png"))


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
        back_light_icon(ICONS / icon_file)
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


# ---------------------------------------------------------------- code hosts
# Each host is searched for public repositories named *-startos. A host that refuses or fails the
# search keeps its repositories from the previous run, so an outage never empties the list.

def gh_token() -> str | None:
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t:
        return t
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


class Forge:
    """A public code host. Subclasses implement search(), root(), raw()."""
    host = name = ""
    headers: dict = {}

    def get(self, url: str, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return http_json(url + q, headers=self.headers)

    def key(self, full_name: str) -> str:
        return f"{self.host}/{full_name}"


class GitHub(Forge):
    host, name = "github.com", "GitHub"

    def __init__(self):
        t = gh_token()
        if not t:
            raise RuntimeError("no GitHub token (set GITHUB_TOKEN or `gh auth login`)")
        self.headers = {"Authorization": f"Bearer {t}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}

    def search(self) -> list[dict]:
        out, page = [], 1
        while True:
            # is:public matters: an authenticated search also returns the token owner's private repos
            r = self.get("https://api.github.com/search/repositories",
                         q="startos in:name is:public", per_page=100, page=page)
            for it in r.get("items", []):
                if it.get("private"):
                    continue
                out.append({"fullName": it["full_name"], "name": it["name"], "pushedAt": it["pushed_at"],
                            "archived": it["archived"], "fork": it["fork"],
                            "description": it.get("description"), "url": it["html_url"]})
            if len(r.get("items", [])) < 100 or page >= 10:
                if r.get("total_count", 0) > 1000:
                    print(f"WARNING: {r['total_count']} GitHub results; search caps at 1000", file=sys.stderr)
                return out
            page += 1

    def root(self, repo: dict) -> set[str]:
        r = self.get(f"https://api.github.com/repos/{repo['fullName']}/contents")
        return {x["name"] for x in r} if isinstance(r, list) else set()

    def raw(self, repo: dict, path: str) -> str | None:
        import base64
        r = self.get(f"https://api.github.com/repos/{repo['fullName']}/contents/{path}")
        if isinstance(r, dict) and r.get("encoding") == "base64":
            return base64.b64decode(r["content"]).decode("utf-8", "replace")
        return None


class GitLab(Forge):
    host, name = "gitlab.com", "GitLab"
    api = "https://gitlab.com/api/v4"

    def search(self) -> list[dict]:
        out, page = [], 1
        while True:
            r = self.get(f"{self.api}/projects", search="startos", per_page=100, page=page)
            for it in r:
                if it.get("visibility", "public") != "public":
                    continue
                out.append({"fullName": it["path_with_namespace"], "name": it["path"],
                            "pushedAt": it["last_activity_at"], "archived": bool(it.get("archived")),
                            "fork": bool(it.get("forked_from_project")), "id": it["id"],
                            "branch": it.get("default_branch"),
                            "description": it.get("description"), "url": it["web_url"]})
            if len(r) < 100 or page >= 10:
                return out
            page += 1

    def root(self, repo: dict) -> set[str]:
        r = self.get(f"{self.api}/projects/{repo['id']}/repository/tree", per_page=100)
        return {x["name"] for x in r}

    def raw(self, repo: dict, path: str) -> str | None:
        url = (f"{self.api}/projects/{repo['id']}/repository/files/"
               f"{urllib.parse.quote(path, safe='')}/raw?ref={urllib.parse.quote(repo.get('branch') or 'HEAD')}")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as f:
            return f.read().decode("utf-8", "replace")


class Gitea(Forge):
    """Gitea / Forgejo API (Codeberg)."""

    def __init__(self, host: str, name: str):
        self.host, self.name, self.api = host, name, f"https://{host}/api/v1"

    def search(self) -> list[dict]:
        out, page = [], 1
        while True:
            r = self.get(f"{self.api}/repos/search", q="startos", limit=50, page=page)
            items = r.get("data", [])
            for it in items:
                if it.get("private") or it.get("internal"):
                    continue
                out.append({"fullName": it["full_name"], "name": it["name"],
                            "pushedAt": it.get("updated_at"), "archived": it.get("archived", False),
                            "fork": it.get("fork", False), "description": it.get("description"),
                            "url": it["html_url"], "branch": it.get("default_branch")})
            if len(items) < 50 or page >= 20:
                return out
            page += 1

    def root(self, repo: dict) -> set[str]:
        r = self.get(f"{self.api}/repos/{repo['fullName']}/contents")
        return {x["name"] for x in r} if isinstance(r, list) else set()

    def raw(self, repo: dict, path: str) -> str | None:
        req = urllib.request.Request(f"{self.api}/repos/{repo['fullName']}/raw/{path}", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as f:
            return f.read().decode("utf-8", "replace")


FORGES = [("GitHub", lambda: GitHub()), ("GitLab", lambda: GitLab()),
          ("Codeberg", lambda: Gitea("codeberg.org", "Codeberg"))]

MANIFEST_PATHS = ["startos/manifest/index.ts", "startos/manifest.ts"]


def ts_field(src: str, field: str) -> str | None:
    m = re.search(rf"\b{field}\s*:\s*(['\"`])(.+?)\1", src)
    return m.group(2) if m else None


def classify(forge: Forge, repo: dict) -> dict:
    base = {k: repo.get(k) for k in ("fullName", "url", "description", "pushedAt")}
    base["forge"] = forge.name
    try:
        names = forge.root(repo)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # empty repository
            return {**base, "kind": "empty"}
        return {**base, "kind": "error", "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {**base, "kind": "error", "error": type(e).__name__}
    if "startos" in names:
        kind = "v04"
    elif "manifest.yaml" in names or "manifest.yml" in names:
        kind = "v03"
    else:
        kind = "other"
    info = {**base, "kind": kind}
    if kind == "v04":
        for p in MANIFEST_PATHS:
            try:
                src = forge.raw(repo, p)
            except Exception:  # noqa: BLE001
                src = None
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


def load_repo_cache() -> dict:
    path = DATA / "repo-cache.json"
    if path.exists():
        return json.loads(path.read_text())
    old = DATA / "github-cache.json"  # earlier, GitHub-only format: bare owner/repo keys
    cache = {}
    if old.exists():
        for fn, v in json.loads(old.read_text()).items():
            cache[f"github.com/{fn}"] = {**v, "forge": "GitHub", "fullName": fn,
                                         "url": f"https://github.com/{fn}"}
        old.unlink()
    return cache


def refresh_forges() -> tuple[dict, dict]:
    cache = load_repo_cache()
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=ACTIVE_DAYS)).isoformat()
    status = {}
    for label, make in FORGES:
        try:
            forge = make()
            found = [r for r in forge.search() if "-startos" in r["name"].lower()]
        except Exception as e:  # noqa: BLE001
            err = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else f"{type(e).__name__}: {e}"
            kept = sum(1 for v in cache.values() if v.get("forge") == label)
            status[label] = {"ok": False, "error": err[:160], "kept": kept}
            print(f"{label}: search failed ({err[:160]}); keeping {kept} cached repos", file=sys.stderr)
            continue
        active = [r for r in found if not r["archived"] and not r["fork"]
                  and (r["pushedAt"] or "") >= cutoff]
        todo = [r for r in active if cache.get(forge.key(r["fullName"]), {}).get("pushedAt") != r["pushedAt"]
                or cache[forge.key(r["fullName"])].get("kind") == "error"]
        with cf.ThreadPoolExecutor(6) as ex:
            for r, info in zip(todo, ex.map(lambda r: classify(forge, r), todo)):
                cache[forge.key(r["fullName"])] = info
        for r in active:  # descriptions can change without a push
            cache[forge.key(r["fullName"])]["description"] = r["description"]
        live = {forge.key(r["fullName"]) for r in active}
        cache = {k: v for k, v in cache.items() if v.get("forge") != label or k in live}
        status[label] = {"ok": True, "repos": len(found), "active": len(active)}
        print(f"{label}: {len(found)} *-startos repos, {len(active)} active, {len(todo)} (re)classified",
              file=sys.stderr)
    write_json(DATA / "repo-cache.json", cache)
    return cache, status


# ---------------------------------------------------------------- merge + render

def build(registries: list[dict], status: dict, cache: dict, curated: dict, forge_status: dict) -> dict:
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
            if m.get("published"):
                s["updated"] = max(filter(None, [s.get("updated"), m["published"]]))
            for c in m["categories"]:
                if c not in s["categories"]:
                    s["categories"].append(c)
            if m["packageRepo"] and m["packageRepo"] not in s["packageRepos"]:
                s["packageRepos"].append(m["packageRepo"])
            s["upstreamRepo"] = s["upstreamRepo"] or m["upstreamRepo"]
            s["short"] = s["short"] or m["short"]
            s["license"] = s["license"] or m["license"]

    def repo_key(url: str | None) -> str:
        return re.sub(r"^https?://", "", url or "").rstrip("/").lower()

    registered = {repo_key(u) for s in services.values() for u in s["packageRepos"]}
    cutoff = (dt.date.today() - dt.timedelta(days=ACTIVE_DAYS)).isoformat()
    for key, info in sorted(cache.items()):
        if info.get("kind") != "v04" or repo_key(info["url"]) in registered:
            continue
        if (info.get("pushedAt") or "")[:10] < cutoff:
            continue  # carried over from a host that failed, but gone stale meanwhile
        man = info.get("manifest") or {}
        repo_id = info["fullName"].rsplit("/", 1)[1].lower().removesuffix("-startos")
        pid = man.get("id") or repo_id
        if pid == "hello-world" and repo_id != "hello-world":
            pid = repo_id  # template id never changed; the repo name is the better guess
        if pid in services and services[pid]["registries"]:
            services[pid]["otherRepos"].append(info["url"])
            continue
        s = services.setdefault(pid, {"id": pid, "title": man.get("title") or pid,
                                      "short": info.get("description"), "license": man.get("license"),
                                      "categories": [], "registries": {}, "packageRepos": [],
                                      "upstreamRepo": None, "otherRepos": [], "lastPush": None,
                                      "forges": []})
        s["packageRepos"].append(info["url"])
        if info["forge"] not in s["forges"]:
            s["forges"].append(info["forge"])
        s["short"] = s["short"] or info.get("description")
        s["upstreamRepo"] = s["upstreamRepo"] or man.get("upstreamRepo")
        s["lastPush"] = max(filter(None, [s.get("lastPush"), info["pushedAt"][:10]]))
        s["updated"] = s["lastPush"]

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
    return {"registries": status, "forges": forge_status, "services": dict(sorted(services.items()))}


def link(url: str | None) -> str:
    if not url:
        return "—"
    label = url.split("github.com/", 1)[1] if "github.com/" in url else re.sub(r"^https?://", "", url)
    return f"[{label}]({url})"


def cell(text: str | None) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def render(result: dict, registries: list[dict], as_of: str, embed: bool = False) -> str:
    head = (ROOT / "templates" / "README.head.md").read_text().rstrip()
    title, _, rest = head.partition("\n")
    names = {r["slug"]: r["name"] for r in registries}
    services = [s for s in result["services"].values() if not s.get("exclude")]
    on_reg = [s for s in services if s["registries"]]
    gh_only = [s for s in services if not s["registries"]]

    site = f"[{SITE_URL.removeprefix('https://').rstrip('/')}]({SITE_URL})"
    parts = [
        title,
        "",
        f"**Browse all {len(services)} services:** {site} (search, filter, sort, choose columns) · "
        "**Download:** [CSV](data/services.csv) · [JSON](data/services.json)",
        "",
        "> [!TIP]",
        "> Looking for something? Open the [service list](" + SITE_URL + ") and type a service name, a "
        "registry name, a category, or a word like *bitcoin* or *photos* into its filter box.",
        rest,
        "",
        f"_List generated {as_of}._",
        "",
        "## Registries",
        "",
        "Every StartOS registry this project knows of. Registry operators can add theirs by pull request; "
        "see [CONTRIBUTING.md](CONTRIBUTING.md). Being listed means only that the registry exists. "
        "**No registry is vetted or endorsed**, and adding one to StartOS trusts its signing keys.",
        "",
        f"_Table as of {as_of}. ⚠️ the registry did not answer at the last check (last known data kept). "
        "<kbd>S9</kbd> run by Start9 · <kbd>3rd</kbd> run by a third party. "
        "Svcs: services installable from it. Categories: services in each, as the registry assigns them._",
        "",
        *registry_table(result, registries, embed),
        "",
        "## Services",
        "",
        f"The full list of {len(services)} services is on the site: {site}. "
        f"{len(on_reg)} are on at least one registry above; {len(gh_only)} are public `*-startos` "
        "repositories that target StartOS 0.4, were pushed to in the last 12 months, and are not on any "
        "registry (you would have to build and sideload these yourself; older 0.3.x wrappers are left "
        "out). The same data is in [`data/services.csv`](data/services.csv) and "
        "[`data/services.json`](data/services.json).",
        "",
        "Code hosts searched:",
        "",
        *forge_lines(result),
        "",
    ]
    return "\n".join(parts)


def forge_lines(result: dict) -> list[str]:
    return [f"- [{label}]({FORGE_URLS[label]}): " + forge_state(result.get("forges", {}).get(label)).replace(
        "*-startos", "`*-startos`") for label, _ in FORGES]


def registry_table(result: dict, registries: list[dict], embed: bool = False) -> list[str]:
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
        def img(file, alt, size):
            ext = file.rsplit(".", 1)[1]
            if embed:
                mime = {"svg": "svg+xml", "jpg": "jpeg"}.get(ext, ext)
                src = f"data:image/{mime};base64," + base64.b64encode((ICONS / file).read_bytes()).decode()
            else:
                src = f"{RAW_BASE}data/registry-icons/{file}"
            return f'<img src="{src}" alt="{alt}" width="{size}" height="{size}">'
        # The icon shares the name's cell: alone in a column, max-width:100% collapses it to 0 px.
        # <kbd> is the one bordered box both GitHub and MarkoPad keep (style attributes are stripped).
        icon = f'<kbd>{img(info["icon"], f"{cell(r["name"])} icon", 40)}</kbd>' if info.get("icon") else ""
        start9 = r["operator"] == "Start9"
        badge = "<kbd>S9</kbd>" if start9 else "<kbd>3rd</kbd>"
        if info.get("description"):
            desc = cell(info["description"].replace("\n\n", "<br>"))
        elif info:
            desc = "_No description provided by the registry owner._"
        else:
            desc = "_Did not answer; description unknown._"
        if r.get("note"):
            desc += f"<br>_Note: {cell(r['note'])}_"
        warn = "" if st.get("ok") else "⚠️ "
        name = f"{warn}{badge} **{cell(r['name'])}**<br>[{r['url']}]({r['url']})<br>{desc}"
        signers = ", ".join(info.get("signers") or []) or "—"
        nb = lambda x: cell(x).replace(" ", "&nbsp;")  # wrap only between categories
        cats = ", ".join(nb(f"{c} ({counts[c]})") for c in sorted(cat_names)) or "—"
        rows.append(f"| {icon} | {name} | {cell(signers)} | {n or '—'} | {cats} |")
    # Invisible header that cannot wrap: it holds the icon column open. An image alone in a column
    # is capped at max-width:100% of a column that then sizes itself to 0 (GitHub and MarkoPad).
    return ["| " + "&nbsp;" * 5 + "Icon" + "&nbsp;" * 5 + " | Registry Name | Trusted signers | Svcs | Categories |",
            "|---|---|---|---|---|", *rows]


def forge_state(st: dict | None) -> str:
    if not st:
        return "not checked yet"
    if st.get("ok"):
        return f"{st['active']} active of {st['repos']} *-startos repositories found"
    return f"⚠️ the search was refused or failed at the last check; {st.get('kept', 0)} repositories kept from earlier"


def site_data(result: dict, registries: list[dict], as_of: str) -> dict:
    """Everything the Pages site shows, with icons embedded so the page stands alone."""
    import base64
    names = {r["slug"]: r["name"] for r in registries}
    regs = []
    for r in registries:
        st = result["registries"].get(r["slug"], {})
        ip, sp = INFO / f"{r['slug']}.json", SNAP / f"{r['slug']}.json"
        info = json.loads(ip.read_text()) if ip.exists() else {}
        snap = json.loads(sp.read_text()) if sp.exists() else {}
        counts = {c: 0 for c in snap.get("__categories__", [])}
        for k, m in snap.items():
            if k != "__categories__":
                for c in m["categories"]:
                    counts[c] = counts.get(c, 0) + 1
        icon = None
        if info.get("icon"):
            ext = info["icon"].rsplit(".", 1)[1]
            mime = {"svg": "svg+xml", "jpg": "jpeg"}.get(ext, ext)
            icon = f"data:image/{mime};base64," + base64.b64encode((ICONS / info["icon"]).read_bytes()).decode()
        regs.append({"name": r["name"], "url": r["url"], "start9": r["operator"] == "Start9",
                     "ok": bool(st.get("ok")), "answered": bool(info), "description": info.get("description"),
                     "signers": info.get("signers") or [], "icon": icon,
                     "services": len([k for k in snap if k != "__categories__"]), "categories": counts})
    services = []
    for s in result["services"].values():
        if s.get("exclude"):
            continue
        on_reg = bool(s["registries"])
        services.append({
            "id": s["id"], "title": s["title"], "short": s.get("short"), "onRegistry": on_reg,
            "where": [names[k] for k in names if k in s["registries"]] if on_reg else (s.get("forges") or ["GitHub"]),
            "packageRepos": s["packageRepos"], "upstream": s.get("upstreamRepo"),
            "replaces": s.get("replaces"), "note": s.get("note"), "categories": s["categories"],
            "license": s.get("license"), "updated": s.get("updated")})
    forges = [{"name": label, "url": FORGE_URLS[label], "state": forge_state(result.get("forges", {}).get(label))}
              for label, _ in FORGES]
    return {"asOf": as_of, "repoUrl": REPO_URL, "registries": regs, "services": services, "forges": forges}


def write_site(data: dict) -> None:
    page = (ROOT / "templates" / "site.html").read_text()
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "index.html").write_text(page.replace("/*DATA*/", blob))
    (ROOT / "docs" / ".nojekyll").write_text("")


def write_csv(data: dict) -> None:
    import csv
    import io
    def safe(v):
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v  # no spreadsheet formulas
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["id", "name", "description", "found_on", "on_registry", "package_repos", "upstream",
                "alternative_to", "note", "categories", "license", "updated"])
    for s in sorted(data["services"], key=lambda s: s["title"].lower()):
        w.writerow([safe(x) for x in [s["id"], s["title"], s["short"], "; ".join(s["where"]),
                    "yes" if s["onRegistry"] else "no", " ".join(s["packageRepos"]), s["upstream"],
                    s["replaces"], s["note"], "; ".join(s["categories"]), s["license"], s["updated"]]])
    (DATA / "services.csv").write_text(buf.getvalue())


def main():
    registries = tomllib.loads((ROOT / "registries.toml").read_text())["registry"]
    curated_path = ROOT / "curated.toml"
    curated = tomllib.loads(curated_path.read_text()) if curated_path.exists() else {}
    offline = "--offline" in sys.argv  # re-render from existing data only
    if offline:
        prev = json.loads((DATA / "services.json").read_text())
        status, forge_status = prev["registries"], prev.get("forges", {})
        cache = load_repo_cache()
    else:
        status = refresh_registries(registries)
        cache, forge_status = refresh_forges()
    result = build(registries, status, cache, curated, forge_status)
    as_of_path = DATA / "as-of.txt"
    data_changed = write_json(DATA / "services.json", result)
    if data_changed or not as_of_path.exists():
        as_of_path.write_text(dt.date.today().isoformat() + "\n")
    as_of = as_of_path.read_text().strip()
    (ROOT / "README.md").write_text(render(result, registries, as_of))
    (ROOT / "README.preview.md").write_text(render(result, registries, as_of, embed=True))
    data = site_data(result, registries, as_of)
    write_site(data)
    write_csv(data)
    missing = [s["id"] for s in result["services"].values()
               if not s.get("exclude") and "replaces" not in s]
    print(f"services: {len(result['services'])}; without a curated 'replaces': {len(missing)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
