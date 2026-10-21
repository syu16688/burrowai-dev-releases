# burrowai-dev-releases

Public, byte-identical mirror of every published [Burrow Dev](https://burrowai.dev) worker release.

Each GitHub Release `v<version>` here carries the same files served at `https://burrowai.dev/releases/`:
`latest.json`, `latest.json.sig` (signed releases), the worker wheel, dependency wheels, and
`release-keys.json` (the release public keys pinned in that wheel).

Workers and Burrow's monitoring compare burrowai.dev against this repository. Burrow's server
and GitHub are separate infrastructure, so a tampered release has to fool both to go unnoticed.

## How releases get here

Burrow Dev's release pipeline uploads **data only**: a tag `v<version>` or `publish/v<version>`
whose root commit holds nothing but the release files under `assets/`. Nothing in a tag is ever
run. The publisher — [`.github/workflows/publish.yml`](.github/workflows/publish.yml) and
[`.github/mirror/github_release.py`](.github/mirror/github_release.py) (standard library + `gh`) —
lives on `main`, which a repository ruleset protects against updates, deletion and force-pushes.
It has no push trigger: it runs on a 5-minute schedule (and manual dispatch), which GitHub always
runs from the default branch, and clones `main` for its code.

For each new tag it reads only `assets/*` and checks:

- the wheel and dependency wheels have the sha256 `latest.json` lists, and `release-keys.json` is
  exactly the key table the wheel pins;
- if [`keys/release-keys.json`](keys/release-keys.json) on `main` lists release keys,
  `latest.json.sig` must verify against one of **those** keys (never keys the upload carries), and
  an unsigned release is refused for any version at or above its `signed_since`. While the table
  is empty, releases are unsigned and their notes say so.

Then tag `v<version>` → **draft** release; tag `publish/v<version>` → re-download every draft
asset, require identical bytes, **publish**. The outcome is recorded as commit status
`burrow-mirror/publish` on the tag's commit. A published release is never overwritten with
different bytes. The workflow uses only its own `GITHUB_TOKEN`.

This repository contains no worker source code.
