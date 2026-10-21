# burrowai-dev-releases

Public, byte-identical mirror of every published [Burrow Dev](https://burrowai.dev) worker release.

Each GitHub Release `v<version>` here carries the same files served at `https://burrowai.dev/releases/`:
`latest.json`, `latest.json.sig` (signed releases), the worker wheel, dependency wheels, and
`release-keys.json` (the release public keys pinned in that wheel).

Workers and Burrow's monitoring compare burrowai.dev against this repository. Burrow's server
and GitHub are separate infrastructure, so a tampered release has to fool both to go unnoticed.

## How releases get here

Burrow Dev's release pipeline pushes a tag whose commit holds the release files under `assets/`
(the commit sits on top of `main` and is reachable only through its tag, so `main` never
carries release files):

- tag `v<version>` → [`.github/workflows/publish.yml`](.github/workflows/publish.yml) creates the
  **draft** release from those files;
- tag `publish/v<version>` → it re-downloads every draft asset, requires identical bytes, and
  **publishes** the release.

A published release is never overwritten with different bytes. The workflow uses only its own
`GITHUB_TOKEN`; the publishing script is [`.github/mirror/github_release.py`](.github/mirror/github_release.py)
(standard library + `gh`).

This repository contains no worker source code.
