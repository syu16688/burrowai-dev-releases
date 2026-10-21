#!/usr/bin/env python3
"""Mirror a published burrowai-dev release as a GitHub Release (tag
v<version>) in the PUBLIC release-only repo syu16688/burrowai-dev-releases —
the independent copy the worker's first-start cross-check
(burrowai_dev.release_trust) and Sam's daily diff compare burrowai.dev
against (DESIGN_e2e_encryption.md §3.3 v6, step 3). Standard library + git +
the `gh` CLI.

Assets, byte-identical to what goes to burrowai.dev:
  latest.json, latest.json.sig (signed releases), the worker wheel, every
  `dep` wheel the manifest lists, and release-keys.json (the key table the
  WHEEL pins + its fingerprint; generated here from the wheel itself).

How a release reaches the mirror (one path for CI and promote_release.sh;
no personal access token anywhere):

  1. `push VERSION DIR --phase draft` (CI with the write deploy key
     MIRROR_DEPLOY_KEY over ssh; Sam's machine with his own git login) —
     refuses if a PUBLISHED v<version> already serves different bytes, then
     makes ONE commit on top of the mirror's main holding assets/<files>,
     .github/mirror/github_release.py (this file) and
     .github/workflows/publish.yml (scripts/mirror/publish.yml), and
     force-pushes it as tag v<version>. The commit is reachable only through
     its tag: main never contains release files (no history bloat; unchanged
     dep wheels are the same git blobs release after release).
  2. The mirror's publish.yml (on: push tags v*, its own GITHUB_TOKEN with
     contents: write) runs `workflow v<version> assets` → `draft`: creates
     (or completes) the DRAFT release from the files in the tag. A draft's
     differing assets are replaced (re-runs rebuild non-reproducible
     wheels); a PUBLISHED release with different bytes is a hard refusal.
     `push` waits for that run and fails if it fails.
  3. After latest.json is live on burrowai.dev: `push VERSION DIR --phase
     publish` pushes the same files as tag publish/v<version>; the mirror
     runs `finalize` (re-download every draft asset, require identical
     bytes, then publish). `push` waits for it, then re-downloads every
     asset from the PUBLIC URL workers use and requires identical bytes.

Every push is a fresh commit, so a re-run always triggers the mirror
workflow; the mirror side is idempotent.

  github_release.py keys-json WHEEL VERSION
      → print release-keys.json for WHEEL
  github_release.py push VERSION DIR --phase draft|publish [--repo R]
      → the git path above (BURROW_MIRROR_GIT overrides the git URL,
        default https://github.com/R.git)
  github_release.py workflow REF DIR [--repo R]
      → mirror side: v<ver> → draft, publish/v<ver> → finalize
  github_release.py draft VERSION DIR [--repo R]
      → mirror side: create/complete the draft from DIR, which must already
        hold release-keys.json (the tag carries it)
  github_release.py publish VERSION DIR [--repo R] [--target SHA]
      → write DIR/release-keys.json, then `draft` directly with gh (manual
        use; CI and promote_release.sh use `push`)
  github_release.py finalize VERSION DIR [--repo R]
      → re-download every asset, require the bytes to equal DIR's, then
        publish the draft.

Never replaces an asset of a PUBLISHED release with different bytes: it
fails loudly instead (exit 1). A draft is not public and no live
latest.json points at it yet, so a re-run replaces a draft's differing
assets.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

HERE = pathlib.Path(__file__).resolve().parent
# The PUBLIC release-only mirror (the source repo syu16688/burrowai-dev is
# private). Same repo as burrowai_dev.release_trust.GITHUB_RELEASES_BASE.
DEFAULT_REPO = "syu16688/burrowai-dev-releases"
ASSET_DIR = "assets"
MIRROR_SCRIPT = ".github/mirror/github_release.py"
MIRROR_WORKFLOW = ".github/workflows/publish.yml"
PUBLISH_TAG_PREFIX = "publish/"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+-]*$")
KEYS_FILE = "release-keys.json"


class Refuse(Exception):
    pass


def _gh() -> str:
    return os.environ.get("GH_BIN", "gh")


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run([_gh(), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise Refuse(f"`gh {' '.join(args)}` failed: {p.stderr.strip() or p.stdout.strip()}")
    return p


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(keys: dict) -> str:
    """Same as burrowai_dev.release_sig.fingerprint (a test keeps them equal)."""
    blob = "".join(f"{k}:{v.lower()}\n" for k, v in sorted(keys.items()))
    h = hashlib.sha256(blob.encode()).hexdigest()[:32]
    return " ".join(h[i:i + 4] for i in range(0, 32, 4))


def _release_keys_module():
    import importlib.util
    path = HERE.parent / "src" / "burrowai_dev" / "release_keys.py"
    spec = importlib.util.spec_from_file_location("_release_keys_gh", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def keys_json(wheel: pathlib.Path, version: str) -> bytes:
    """release-keys.json for a built wheel: the key table it pins, read out
    of the wheel (never imported). Deterministic bytes."""
    try:
        keys = _release_keys_module().pinned_keys_in_wheel(wheel)
    except Exception as e:  # noqa: BLE001
        raise Refuse(f"{wheel.name}: can't read its release keys ({e})") from None
    keys = {k: v.lower() for k, v in keys.items()}
    doc = {"product": "burrowai-dev", "version": version, "keys": keys,
           "fingerprint": fingerprint(keys) if keys else None}
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()


def _basename(url: str) -> str:
    name = pathlib.PurePosixPath(urlparse(url).path).name
    if not _SAFE_NAME.match(name):
        raise Refuse(f"unsafe artifact filename in {url!r}")
    return name


def _manifest_files(version: str, d: pathlib.Path) -> list[tuple[str, str]]:
    """[(filename, sha256)] — the worker wheel first, then each dep — from
    DIR/latest.json, which must be for VERSION."""
    if not _VERSION.match(version):
        raise Refuse(f"bad version {version!r}")
    try:
        m = json.loads((d / "latest.json").read_bytes())
    except (OSError, ValueError) as e:
        raise Refuse(f"{d / 'latest.json'}: not a readable manifest ({e})") from None
    if not isinstance(m, dict) or m.get("version") != version:
        raise Refuse(f"latest.json is not for {version}")
    try:
        files = [(_basename(m["wheel_url"]), m["sha256"])]
        for a in m.get("artifacts") or []:
            if isinstance(a, dict) and a.get("kind") == "dep":
                files.append((_basename(a["url"]), a["sha256"]))
    except (KeyError, TypeError) as e:
        raise Refuse(f"malformed latest.json ({e})") from None
    return files


def assets(version: str, d: pathlib.Path) -> list[str]:
    """Asset filenames for the release in DIR, in upload order. Checks that
    every file exists and the wheel + each dep have the sha256 latest.json
    says."""
    files = _manifest_files(version, d)
    for name, want in files:
        p = d / name
        if not p.is_file():
            raise Refuse(f"{name} is listed in latest.json but missing from {d}")
        if _sha(p) != str(want).lower():
            raise Refuse(f"{name}: sha256 differs from latest.json")
    out = ["latest.json"]
    if (d / "latest.json.sig").is_file():
        out.append("latest.json.sig")
    out += [n for n, _ in files]
    out.append(KEYS_FILE)
    return out


def _view(tag: str, repo: str) -> dict | None:
    p = _run(["release", "view", tag, "--repo", repo, "--json", "isDraft,assets"], check=False)
    if p.returncode != 0:
        err = (p.stderr + p.stdout).lower()
        if "not found" in err or "could not find" in err:
            return None
        raise Refuse(f"`gh release view {tag}` failed: {p.stderr.strip()}")
    return json.loads(p.stdout)


def _download(tag: str, repo: str, name: str, dest: pathlib.Path) -> pathlib.Path:
    _run(["release", "download", tag, "--repo", repo, "--pattern", name, "-D", str(dest),
          "--clobber"])
    got = dest / name
    if not got.is_file():
        raise Refuse(f"downloaded {name} from {tag} but it isn't there")
    return got


def _require_keys(version: str, d: pathlib.Path) -> None:
    """DIR/release-keys.json must be there (the mirror can't regenerate it:
    it has no burrowai_dev source) and be for VERSION."""
    try:
        doc = json.loads((d / KEYS_FILE).read_bytes())
    except (OSError, ValueError) as e:
        raise Refuse(f"{d / KEYS_FILE}: missing or unreadable ({e})") from None
    if not isinstance(doc, dict) or doc.get("version") != version \
            or not isinstance(doc.get("keys"), dict):
        raise Refuse(f"{KEYS_FILE} is not a key table for {version}")


def draft(version: str, d: pathlib.Path, repo: str, target: str | None = None,
          *, verify_tag: bool = True) -> int:
    """Create (or complete) the DRAFT release v<version> from DIR, which
    already holds release-keys.json. With verify_tag the tag must already
    exist (the mirror workflow: the tag IS the pushed asset commit)."""
    tag = f"v{version}"
    names = assets(version, d)
    _require_keys(version, d)
    rel = _view(tag, repo)
    if rel is None:
        fp = json.loads((d / KEYS_FILE).read_bytes()).get("fingerprint")
        notes = (f"Mirror of https://burrowai.dev/releases/ for {version}. "
                 "Byte-identical copies, published as an independent check "
                 "(DESIGN_e2e_encryption.md §3.3).")
        if fp:
            notes += f"\n\nRelease key fingerprint: `{fp}` (also in release-keys.json)."
        args = ["release", "create", tag, "--repo", repo, "--draft",
                "--title", f"burrowai-dev {version}", "--notes", notes]
        if verify_tag:
            args.append("--verify-tag")
        if target:
            args += ["--target", target]
        _run(args + [str(d / n) for n in names])
        print(f"✓ created draft GitHub release {tag} with {len(names)} assets")
        return 0
    draft_ = bool(rel.get("isDraft"))
    have = {a.get("name") for a in rel.get("assets") or []}
    with tempfile.TemporaryDirectory() as tmp:
        missing, differ = [], []
        for n in names:
            if n not in have:
                missing.append(n)
            elif _sha(_download(tag, repo, n, pathlib.Path(tmp))) != _sha(d / n):
                differ.append(n)
    if differ and not draft_:
        raise Refuse(f"GitHub release {tag} is already published with DIFFERENT bytes for "
                     f"{', '.join(differ)} — refusing to overwrite. Investigate before "
                     "publishing this version anywhere.")
    if differ:
        _run(["release", "upload", tag, "--repo", repo, "--clobber",
              *[str(d / n) for n in differ]])
        print(f"   replaced draft assets: {', '.join(differ)}")
    if missing:
        _run(["release", "upload", tag, "--repo", repo, *[str(d / n) for n in missing]])
        print(f"   added: {', '.join(missing)}")
    state = "draft" if draft_ else "published"
    print(f"✓ GitHub release {tag} ({state}) matches {d}")
    return 0


def publish(version: str, d: pathlib.Path, repo: str, target: str | None) -> int:
    """Manual path straight through gh: release-keys.json from the wheel,
    then the draft (the tag is created at --target if it doesn't exist)."""
    assets(version, d)
    wheel = d / _manifest_files(version, d)[0][0]
    (d / KEYS_FILE).write_bytes(keys_json(wheel, version))
    return draft(version, d, repo, target, verify_tag=False)


def finalize(version: str, d: pathlib.Path, repo: str) -> int:
    tag = f"v{version}"
    names = assets(version, d)
    rel = _view(tag, repo)
    if rel is None:
        raise Refuse(f"GitHub release {tag} doesn't exist — run `publish` first")
    have = {a.get("name") for a in rel.get("assets") or []}
    absent = [n for n in names if n not in have]
    if absent:
        raise Refuse(f"GitHub release {tag} lacks {', '.join(absent)}")
    with tempfile.TemporaryDirectory() as tmp:
        bad = [n for n in names
               if _sha(_download(tag, repo, n, pathlib.Path(tmp))) != _sha(d / n)]
    if bad:
        raise Refuse(f"GitHub release {tag} differs from what was published for "
                     f"{', '.join(bad)}")
    if rel.get("isDraft"):
        _run(["release", "edit", tag, "--repo", repo, "--draft=false"])
        print(f"✓ published GitHub release {tag}")
    else:
        print(f"✓ GitHub release {tag} already published and identical")
    return 0


# ─── the git path (CI + promote_release.sh → the mirror's workflow) ───

def _git(args: list[str], *, cwd: pathlib.Path | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        raise Refuse(f"`git {' '.join(args)}` failed: {p.stderr.strip() or p.stdout.strip()}")
    return p


def _git_url(repo: str) -> str:
    return os.environ.get("BURROW_MIRROR_GIT") or f"https://github.com/{repo}.git"


def _public_base(repo: str) -> str:
    return (os.environ.get("BURROW_MIRROR_PUBLIC_BASE")
            or f"https://github.com/{repo}/releases/download")


def _fetch_public(url: str) -> bytes | None:
    """Bytes at a public URL; None if it isn't there (404). Anything else —
    network trouble — is a refusal: we can't tell what's published."""
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise Refuse(f"{url}: HTTP {e.code}") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, FileNotFoundError):       # file:// (tests)
            return None
        raise Refuse(f"{url}: {e.reason}") from None
    except OSError as e:
        raise Refuse(f"{url}: {e}") from None


def public_state(version: str, d: pathlib.Path, names: list[str], repo: str) -> str:
    """What the PUBLIC copy of v<version> serves, against DIR:
    'absent' (none of it), 'partial' (some, all identical), 'identical'
    (all, identical). Raises Refuse if any public asset differs — a
    published release is never overwritten."""
    base = f"{_public_base(repo)}/v{version}"
    found, differ = 0, []
    for n in names:
        got = _fetch_public(f"{base}/{n}")
        if got is None:
            continue
        found += 1
        if hashlib.sha256(got).hexdigest() != _sha(d / n):
            differ.append(n)
    if differ:
        raise Refuse(f"the PUBLIC GitHub release v{version} already serves DIFFERENT bytes "
                     f"for {', '.join(differ)} — refusing. Investigate before publishing "
                     "this version anywhere.")
    if found == 0:
        return "absent"
    return "identical" if found == len(names) else "partial"


def _runs_for(repo: str, sha: str) -> list[dict]:
    """The mirror's workflow runs for commit SHA (public repo: gh's token, or
    unauthenticated if that is refused)."""
    path = f"repos/{repo}/actions/runs?head_sha={sha}&event=push&per_page=20"
    p = _run(["api", path], check=False)
    if p.returncode == 0:
        try:
            return list(json.loads(p.stdout).get("workflow_runs") or [])
        except ValueError:
            pass
    api = os.environ.get("BURROW_MIRROR_API", "https://api.github.com")
    req = urllib.request.Request(f"{api}/{path}",
                                 headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return list(json.loads(r.read()).get("workflow_runs") or [])
    except Exception:  # noqa: BLE001 — transient; the caller keeps polling
        return []


def _wait_for_mirror(repo: str, sha: str, ref: str) -> None:
    """Block until the mirror's publish.yml run for commit SHA finishes;
    Refuse unless it succeeded."""
    timeout = float(os.environ.get("BURROW_MIRROR_WAIT", "900"))
    poll = float(os.environ.get("BURROW_MIRROR_POLL", "10"))
    deadline = time.monotonic() + timeout
    while True:
        runs = [r for r in _runs_for(repo, sha)
                if r.get("head_sha") == sha
                and MIRROR_WORKFLOW in str(r.get("path", MIRROR_WORKFLOW))]
        done = [r for r in runs if r.get("status") == "completed"]
        if done:
            r = done[0]
            if r.get("conclusion") != "success":
                raise Refuse(f"the mirror's publish run for {ref} ended "
                             f"'{r.get('conclusion')}': {r.get('html_url', '')}")
            print(f"   mirror run for {ref} succeeded")
            return
        if time.monotonic() >= deadline:
            state = runs[0].get("status") if runs else "not started"
            raise Refuse(f"the mirror's publish run for {ref} didn't finish in "
                         f"{int(timeout)}s ({state})")
        time.sleep(poll)


def _asset_commit(version: str, d: pathlib.Path, names: list[str], repo: str,
                  phase: str, work: pathlib.Path) -> str:
    """One commit on top of the mirror's main with assets/ = exactly NAMES
    from DIR, plus the mirror's script + workflow from this checkout. → sha"""
    url = _git_url(repo)
    _git(["init", "-q", str(work)])
    _git(["fetch", "-q", "--depth", "1", url, "refs/heads/main"], cwd=work)
    _git(["checkout", "-q", "--detach", "FETCH_HEAD"], cwd=work)
    shutil.rmtree(work / ASSET_DIR, ignore_errors=True)
    (work / ASSET_DIR).mkdir()
    for n in names:
        shutil.copyfile(d / n, work / ASSET_DIR / n)
    for src, dst in ((pathlib.Path(__file__).resolve(), MIRROR_SCRIPT),
                     (HERE / "mirror" / "publish.yml", MIRROR_WORKFLOW)):
        (work / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, work / dst)
    _git(["add", "-A"], cwd=work)
    _git(["-c", "user.name=burrowai-dev release", "-c", "user.email=releases@burrowai.dev",
          "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty",
          "-m", f"burrowai-dev {version} ({phase})"], cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work).stdout.strip()


def push(version: str, d: pathlib.Path, repo: str, phase: str) -> int:
    if phase not in ("draft", "publish"):
        raise Refuse(f"--phase must be draft or publish, not {phase!r}")
    tag = f"v{version}"
    names = assets(version, d)
    if phase == "draft":
        wheel = d / _manifest_files(version, d)[0][0]
        (d / KEYS_FILE).write_bytes(keys_json(wheel, version))
    else:
        _require_keys(version, d)        # the very file the draft carried
    state = public_state(version, d, names, repo)
    if state == "identical":
        print(f"✓ GitHub release {tag} is already public and identical")
        return 0
    if phase == "publish" and state == "partial":
        raise Refuse(f"GitHub release {tag} is public but incomplete — re-run the draft "
                     "phase first")
    ref = tag if phase == "draft" else f"{PUBLISH_TAG_PREFIX}{tag}"
    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp) / "mirror"
        sha = _asset_commit(version, d, names, repo, phase, work)
        _git(["push", "-q", "--force", _git_url(repo), f"HEAD:refs/tags/{ref}"], cwd=work)
    print(f"   pushed {ref} → {sha[:12]} ({len(names)} assets)")
    _wait_for_mirror(repo, sha, ref)
    if phase == "draft":
        print(f"✓ draft GitHub release {tag} matches {d}")
        return 0
    tries = int(os.environ.get("BURROW_MIRROR_PUBLIC_TRIES", "6"))
    for i in range(tries):
        if public_state(version, d, names, repo) == "identical":
            print(f"✓ GitHub release {tag} is public and byte-identical")
            return 0
        if i + 1 < tries:
            time.sleep(float(os.environ.get("BURROW_MIRROR_POLL", "10")))
    raise Refuse(f"GitHub release {tag} was published but its public copy doesn't serve "
                 "every asset yet")


def workflow(ref: str, d: pathlib.Path, repo: str) -> int:
    """The mirror's publish.yml: v<ver> → draft, publish/v<ver> → finalize."""
    if ref.startswith(PUBLISH_TAG_PREFIX + "v"):
        return finalize(ref[len(PUBLISH_TAG_PREFIX) + 1:], d, repo)
    if ref.startswith("v"):
        return draft(ref[1:], d, repo, verify_tag=True)
    raise Refuse(f"not a release tag: {ref!r}")


def _opt(argv: list[str], flag: str, default=None):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 >= len(argv):
            raise Refuse(f"{flag} needs a value")
        val = argv[i + 1]
        del argv[i:i + 2]
        return val
    return default


def main(argv: list[str]) -> int:
    argv = list(argv[1:])
    try:
        repo = _opt(argv, "--repo", os.environ.get("BURROW_MIRROR_REPO", DEFAULT_REPO))
        target = _opt(argv, "--target")
        phase = _opt(argv, "--phase")
        if len(argv) == 3 and argv[0] == "keys-json":
            sys.stdout.write(keys_json(pathlib.Path(argv[1]), argv[2]).decode())
            return 0
        if len(argv) == 3 and argv[0] == "push" and phase:
            return push(argv[1], pathlib.Path(argv[2]), repo, phase)
        if len(argv) == 3 and argv[0] == "workflow":
            return workflow(argv[1], pathlib.Path(argv[2]), repo)
        if len(argv) == 3 and argv[0] == "draft":
            return draft(argv[1], pathlib.Path(argv[2]), repo, target)
        if len(argv) == 3 and argv[0] == "publish":
            return publish(argv[1], pathlib.Path(argv[2]), repo, target)
        if len(argv) == 3 and argv[0] == "finalize":
            return finalize(argv[1], pathlib.Path(argv[2]), repo)
    except Refuse as e:
        print(f"✗ REFUSING: {e}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
