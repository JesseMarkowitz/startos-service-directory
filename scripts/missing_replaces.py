#!/usr/bin/env python3
"""Print the Markdown for the rolling PR body: how many listed services still have no curated
`replaces`, and a ready-to-fill curated.toml block for each one.

Each block carries the service's description, its upstream's own GitHub description (fetched
here, one API call per GitHub-hosted upstream) and both repository links, so filling it in is a
decision rather than a research task. Nothing is written to the repository: values stay empty,
and a human fills them in and commits them to curated.toml.

Run after scripts/refresh.py, which writes data/services.json.
"""
from __future__ import annotations

import json
import re

from refresh import DATA, gh_token, http_json

MAX_BLOCKS = 100  # keeps the PR body well under GitHub's 65,536-character limit


def upstream_description(url: str | None, token: str | None) -> str | None:
    m = re.match(r"https?://github\.com/([^/]+/[^/#?]+)", url or "")
    if not m or not token:
        return None
    try:
        repo = http_json(f"https://api.github.com/repos/{m.group(1).removesuffix('.git')}",
                         headers={"Authorization": f"Bearer {token}",
                                  "Accept": "application/vnd.github+json"}, timeout=20)
    except Exception:  # noqa: BLE001 — a missing description never fails the run
        return None
    return (repo.get("description") or "").strip() or None


def one_line(text: str | None) -> str:
    return " ".join((text or "").split()) or "(none)"


def main() -> None:
    services = json.loads((DATA / "services.json").read_text())["services"]
    missing = [s for s in services.values() if not s.get("exclude") and not s.get("replaces")]
    print(f'Services without a curated "replaces" entry: {len(missing)}')
    if not missing:
        return
    token = gh_token()
    blocks = []
    for s in missing[:MAX_BLOCKS]:
        lines = [f"# {s['title']}: {one_line(s.get('short'))}"]
        up = s.get("upstreamRepo")
        if up:
            lines.append(f"#   upstream: {up}")
            lines.append(f"#   upstream says: {one_line(upstream_description(up, token))}")
        for repo in s.get("packageRepos") or []:
            lines.append(f"#   package: {repo}")
        lines += [f"[{s['id']}]", 'replaces = ""   # paid service it replaces, or "—" if none']
        blocks.append("\n".join(lines))
    print()
    print("Fill these in and add them to `curated.toml` in a separate commit on `main` "
          "(the next run picks them up):")
    print()
    print("```toml")
    print("\n\n".join(blocks))
    print("```")
    if len(missing) > MAX_BLOCKS:
        print(f"\n…and {len(missing) - MAX_BLOCKS} more; run `python3 scripts/missing_replaces.py` locally.")


if __name__ == "__main__":
    main()
