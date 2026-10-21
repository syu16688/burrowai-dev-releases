# burrowai-dev-releases

Public, byte-identical mirror of every published [Burrow Dev](https://burrowai.dev) worker release.

Each GitHub Release `v<version>` here carries the same files served at `https://burrowai.dev/releases/`:
`latest.json`, `latest.json.sig` (signed releases), the worker wheel, dependency wheels, and
`release-keys.json` (the release public keys pinned in that wheel).

Workers and Burrow's monitoring compare burrowai.dev against this repository. Burrow's server
and GitHub are separate infrastructure, so a tampered release has to fool both to go unnoticed.

This repository contains no source code.
