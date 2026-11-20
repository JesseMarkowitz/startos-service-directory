# Contributing

`README.md` is generated, so don't edit it. Change the source files
below and say what you changed in the pull request. A maintainer reruns `scripts/refresh.py`
before merging.

## Add a registry

Add a block to [`registries.toml`](registries.toml):

```toml
[[registry]]
slug = "my-registry"              # short, lowercase, unique
name = "My Registry"              # what StartOS shows
url = "https://registry.example.com/"
operator = "3rdParty"             # "Start9" or "3rdParty"
note = "Bitcoin tools I maintain" # optional, one line
```

Requirements:

- The URL answers the StartOS registry API (`<url>/rpc/v0`, method `package.index`) over HTTPS.
- It's a public registry anyone can add, not a personal test registry.
- You run it, or you link where its operator announced it.

Listing a registry is not an endorsement. Registries that stop answering for a long time, or that
are reported as serving malicious packages, are removed.

## Add or correct a service

- **"Open-source alternative to", notes, or a missing upstream link:** edit
  [`curated.toml`](curated.toml): `replaces = "..."`, `note = "..."`, `upstream = "https://..."`.
- **Not showing up:** services come from the registries and from public GitHub repositories
  named `*-startos` that target StartOS 0.4 and were pushed to in the last 12 months. Fix the
  package's manifest, or open an issue.
- **Removal:** if you maintain a package or registry and want it off the list, open an issue.
