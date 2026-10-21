#!/usr/bin/env python3
"""Mirror a published burrowai-dev release as a GitHub Release (tag
v<version>) in the PUBLIC release-only repo syu16688/burrowai-dev-releases —
the independent copy the worker's first-start cross-check
(burrowai_dev.release_trust) and Sam's daily diff compare burrowai.dev
against (DESIGN_e2e_encryption.md §3.3 v6, step 3). Standard library + git +
the `gh` CLI. This one file is also the mirror's PUBLISHER: a copy lives on
the mirror's protected main as .github/mirror/github_release.py.

Assets, byte-identical to what goes to burrowai.dev:
  latest.json, latest.json.sig (signed releases), the worker wheel, every
  `dep` wheel the manifest lists, and release-keys.json (the key table the
  WHEEL pins + its fingerprint; generated here from the wheel itself).

Who controls what (Codex r12 P1): the uploader supplies DATA only, never
code, and never writes the mirror at all. Uploads go to a separate PUBLIC
DROP repo, syu16688/burrowai-dev-releases-drop, which has GitHub Actions
DISABLED (nothing pushed there is ever executed — a tag carrying its own
.github/workflows/x.yml with a push trigger can't run, which a tag on the
mirror itself could, with self-granted contents: write). CI's write deploy
key (MIRROR_DEPLOY_KEY) exists only on the drop repo; the mirror has no
deploy key. The code that runs with the mirror's `contents: write` token
comes only from the mirror's main, which a repository ruleset
(burrow-mirror-main) makes unwritable by Actions and by anyone without
changing the ruleset:

  1. `push VERSION DIR --phase draft` (CI with the deploy key over ssh;
     Sam's machine with his own git login) refuses if a PUBLISHED
     v<version> already serves different bytes, then pushes ONE root commit
     holding nothing but assets/<files> as tag v<version> to the DROP repo
     (data; no workflow, no script).
  2. The mirror's publish.yml runs on `schedule` (every 5 min) and
     `workflow_dispatch` only — both always use the workflow on the default
     branch; there is no push trigger. It clones main and runs `scan`, which
     lists the drop repo's tags v<semver> / publish/v<semver> over
     unauthenticated public git (no token is ever sent to the drop repo;
     anything else is ignored), reads ONLY the assets/* blobs of each new
     tag commit as data,
     verifies them (`verify_release`: wheel/dep sha256 against latest.json,
     release-keys.json against the wheel, and — once the key table on main,
     keys/release-keys.json, has keys — latest.json.sig against THAT table;
     an unsigned release for a version >= the table's `signed_since` is
     refused) and drafts (v<ver>) or publishes (publish/v<ver>) the release.
     It records the outcome in the MIRROR as results/<tag commit sha>.json
     on the mirror's `results` branch (written with the workflow's own
     GITHUB_TOKEN; nobody else but Sam can write the mirror); `push` polls
     that file (public read) and fails unless it says success.
  3. After latest.json is live on burrowai.dev: `push VERSION DIR --phase
     publish` pushes the same files as tag publish/v<version>; the scan
     re-verifies, re-downloads every draft asset, requires identical bytes
     and publishes. `push` then re-downloads every asset from the PUBLIC URL
     workers use and requires identical bytes.

Mirror main is changed only through the admin path (Sam's own gh login,
repository admin): the ruleset grants the repository-admin role a bypass
for the duration of the write and takes it away again (verified), so the
deploy key and Actions stay locked out throughout.

  github_release.py keys-json WHEEL VERSION
      → print release-keys.json for WHEEL
  github_release.py push VERSION DIR --phase draft|publish [--repo R]
      → the git path above: tags go to the drop repo (BURROW_MIRROR_DROP_REPO,
        default R-drop; BURROW_MIRROR_DROP_GIT overrides its git URL, default
        https://github.com/<drop>.git)
  github_release.py scan [--repo R] [--keys PATH]
      → mirror side (publish.yml): process every new release tag
  github_release.py sync-keys VERSION DIR [--repo R]
      → admin: make keys/release-keys.json on mirror main the key table the
        release in DIR pins (first signed release / rotation); no-op if equal
  github_release.py update-publisher [--repo R]
      → admin: put this file + scripts/mirror/publish.yml on mirror main
  github_release.py protect-mirror [--repo R]
      → admin: create / re-assert the ruleset protecting mirror main
  github_release.py check-setup [--repo R]
      → admin: verify the trust setup — drop repo public with Actions
        disabled and exactly one (write) deploy key, mirror with NO deploy
        key, ruleset in force
  github_release.py draft VERSION DIR [--repo R]
      → create/complete the draft from DIR (must hold release-keys.json)
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

import ast
import base64
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
import zipfile
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
# Release versions are plain MAJOR.MINOR.PATCH; the only tags the mirror's
# publisher acts on are v<that> and publish/v<that>.
_SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
_VERSION = re.compile(rf"^{_SEMVER}$")
_TAG = re.compile(rf"^(publish/)?v({_SEMVER})$")
KEYS_FILE = "release-keys.json"
# On mirror main (protected): the release keys the publisher verifies
# latest.json.sig against. {"keys": {}} until the first signed release.
KEY_TABLE = "keys/release-keys.json"
PRODUCT = "burrowai-dev"
STATUS_CONTEXT = "burrow-mirror/publish"
# The publisher's results: results/<tag commit sha>.json on this branch of
# the MIRROR (not the drop repo — only the mirror's workflow writes there).
RESULTS_BRANCH = "results"
RESULTS_DIR = "results"
_SHA = re.compile(r"^[0-9a-f]{40}$")
RULESET_NAME = "burrow-mirror-main"
# Repository admin (Sam's own login). Granted a bypass only while the admin
# path writes main, then removed again.
ADMIN_BYPASS = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
RULESET = {
    "name": RULESET_NAME, "target": "branch", "enforcement": "active", "bypass_actors": [],
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
    "rules": [{"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}],
}


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


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KEYS_MEMBER = "burrowai_dev/release_keys.py"


def keys_in_source(text: str) -> dict[str, str]:
    """The RELEASE_PUBKEYS literal of a release_keys.py, never executed.
    Same as burrowai_dev.release_keys.keys_in_source (a test keeps them
    equal); copied because the mirror's publisher has only this file."""
    for node in ast.walk(ast.parse(text)):
        target = value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target == "RELEASE_PUBKEYS":
            keys = ast.literal_eval(value)
            if not isinstance(keys, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) and _HEX64.match(v.lower())
                    for k, v in keys.items()):
                raise ValueError("RELEASE_PUBKEYS must map key ids to 64-hex public keys")
            return keys
    raise ValueError("no RELEASE_PUBKEYS assignment")


def pinned_keys(wheel: pathlib.Path) -> dict[str, str]:
    """The release keys a burrowai-dev wheel pins ({} = none), lower-case.
    Read out of the zip, never imported."""
    try:
        with zipfile.ZipFile(wheel) as z:
            try:
                text = z.read(_KEYS_MEMBER).decode("utf-8")
            except KeyError:
                return {}
        return {k: v.lower() for k, v in keys_in_source(text).items()}
    except Exception as e:  # noqa: BLE001
        raise Refuse(f"{wheel.name}: can't read its release keys ({e})") from None


def keys_json(wheel: pathlib.Path, version: str) -> bytes:
    """release-keys.json for a built wheel: the key table it pins, read out
    of the wheel (never imported). Deterministic bytes."""
    keys = pinned_keys(wheel)
    doc = {"product": PRODUCT, "version": version, "keys": keys,
           "fingerprint": fingerprint(keys) if keys else None}
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()


# ─── Ed25519 verification (RFC 8032 §6; same code as release_sig) ────
# Stdlib only: the mirror's publisher runs this one file.

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _ed_add(P, Q):
    a = (P[1] - P[0]) * (Q[1] - Q[0]) % _P
    b = (P[1] + P[0]) * (Q[1] + Q[0]) % _P
    c = 2 * P[3] * Q[3] * _D % _P
    d = 2 * P[2] * Q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _ed_mul(s: int, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _ed_add(Q, P)
        P = _ed_add(P, P)
        s >>= 1
    return Q


def _ed_equal(P, Q) -> bool:
    return ((P[0] * Q[2] - Q[0] * P[2]) % _P == 0
            and (P[1] * Q[2] - Q[1] * P[2]) % _P == 0)


def _recover_x(y: int, sign: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def ed25519_verify(public: bytes, msg: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    A, R = _decompress(public), _decompress(signature[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + msg).digest(), "little") % _L
    return _ed_equal(_ed_mul(s, _G), _ed_add(R, _ed_mul(h, A)))


def _basename(url: str) -> str:
    name = pathlib.PurePosixPath(urlparse(url).path).name
    if not _SAFE_NAME.match(name):
        raise Refuse(f"unsafe artifact filename in {url!r}")
    return name


def _semver(version: str) -> tuple[int, int, int]:
    m = _VERSION.match(version or "")
    if not m:
        raise Refuse(f"bad version {version!r} (want MAJOR.MINOR.PATCH)")
    return tuple(int(x) for x in m.groups())


def _manifest_files(version: str, d: pathlib.Path) -> list[tuple[str, str]]:
    """[(filename, sha256)] — the worker wheel first, then each dep — from
    DIR/latest.json, which must be for VERSION."""
    _semver(version)
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


def empty_table() -> bytes:
    """keys/release-keys.json before any release key exists."""
    return (json.dumps({"product": PRODUCT, "keys": {}, "signed_since": None},
                       indent=2, sort_keys=True) + "\n").encode()


def parse_table(raw: bytes) -> dict:
    """The key table on mirror main. Anything malformed is a refusal — the
    publisher fails closed rather than guess which releases need a
    signature."""
    try:
        t = json.loads(raw)
    except ValueError as e:
        raise Refuse(f"{KEY_TABLE}: not JSON ({e})") from None
    if not isinstance(t, dict) or t.get("product") != PRODUCT \
            or not isinstance(t.get("keys"), dict):
        raise Refuse(f"{KEY_TABLE}: not a {PRODUCT} key table")
    keys = t["keys"]
    if not all(isinstance(k, str) and _SAFE_NAME.match(k) and isinstance(v, str)
               and _HEX64.match(v) for k, v in keys.items()):
        raise Refuse(f"{KEY_TABLE}: keys must map key ids to lower-case 64-hex public keys")
    since = t.get("signed_since")
    if keys:
        if not isinstance(since, str):
            raise Refuse(f"{KEY_TABLE}: has keys but no signed_since version")
        _semver(since)
    elif since is not None:
        raise Refuse(f"{KEY_TABLE}: signed_since without keys")
    return {"keys": dict(keys), "signed_since": since}


def verify_release(version: str, d: pathlib.Path, table: dict) -> str:
    """The mirror publisher's checks on the release files in DIR (read from
    a tag as data), against the key table from mirror main. Returns the
    release-notes line describing the signature; raises Refuse.

      - every listed file is there with the sha256 latest.json says, and
        latest.json is for VERSION (assets);
      - release-keys.json is exactly the key table the wheel pins;
      - table empty (no release key exists yet): the release must be
        unsigned (no .sig, wheel pins nothing) and is marked `unsigned`;
      - table has keys: a latest.json.sig must verify against the TABLE's
        keys (not the release's own), with the manifest's product, key_id
        (= the signer) and version right; a release without one is refused
        for any version >= the table's signed_since."""
    names = assets(version, d)
    _require_keys(version, d)
    wheel = d / _manifest_files(version, d)[0][0]
    wheel_keys = pinned_keys(wheel)
    if (d / KEYS_FILE).read_bytes() != keys_json(wheel, version):
        raise Refuse(f"{KEYS_FILE} is not the key table {wheel.name} pins")
    raw = (d / "latest.json").read_bytes()
    keys, since = table["keys"], table["signed_since"]
    if "latest.json.sig" not in names:
        if not keys:
            if wheel_keys:
                raise Refuse(f"{wheel.name} pins release keys but the mirror's key table "
                             f"({KEY_TABLE} on main) is empty — run `sync-keys` first")
            return ("**unsigned** — published before any release key existed "
                    f"({KEY_TABLE} on the mirror's main is empty).")
        if _semver(version) >= _semver(since):
            raise Refuse(f"{version} has no latest.json.sig; releases since {since} must be "
                         "signed")
        if wheel_keys:
            raise Refuse(f"{wheel.name} pins release keys but latest.json is unsigned")
        return f"**unsigned** — predates signed releases (signed since {since})."
    if not keys:
        raise Refuse(f"{version} is signed but the mirror's key table ({KEY_TABLE} on main) "
                     "is empty — run `sync-keys` first")
    try:
        sig = base64.b64decode((d / "latest.json.sig").read_text().strip(), validate=True)
    except Exception as e:  # noqa: BLE001
        raise Refuse(f"latest.json.sig is not base64 ({e})") from None
    signer = next((kid for kid, pub in sorted(keys.items())
                   if ed25519_verify(bytes.fromhex(pub), raw, sig)), None)
    if signer is None:
        raise Refuse(f"latest.json.sig for {version} doesn't verify against any key in "
                     f"{KEY_TABLE} on the mirror's main")
    def no_dup(pairs):
        if len({k for k, _ in pairs}) != len(pairs):
            raise Refuse("signed manifest has duplicate keys")
        return dict(pairs)
    m = json.loads(raw, object_pairs_hook=no_dup)
    if m.get("product") != PRODUCT:
        raise Refuse(f"signed manifest product {m.get('product')!r} is not {PRODUCT!r}")
    if m.get("key_id") != signer:
        raise Refuse(f"signed manifest key_id {m.get('key_id')!r} != signing key {signer!r}")
    return (f"Signed: latest.json.sig verified by the mirror's publisher against "
            f"{KEY_TABLE} on its protected main (key `{signer}`, table fingerprint "
            f"`{fingerprint(keys)}`).")


def draft(version: str, d: pathlib.Path, repo: str, target: str | None = None,
          *, verify_tag: bool = True, notes_extra: str = "") -> int:
    """Create (or complete) the DRAFT release v<version> from DIR, which
    already holds release-keys.json. With verify_tag the tag must already
    exist; otherwise it is created at TARGET when the release is published
    (the mirror workflow: target main — the upload tag is in the drop repo)."""
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
        if notes_extra:
            notes += f"\n\n{notes_extra}"
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
    """The MIRROR's git URL (its main: the publisher's code, read only)."""
    return os.environ.get("BURROW_MIRROR_GIT") or f"https://github.com/{repo}.git"


def _drop_repo(repo: str) -> str:
    """The public upload drop box next to mirror REPO (Actions disabled)."""
    return os.environ.get("BURROW_MIRROR_DROP_REPO") or f"{repo}-drop"


def _drop_url(repo: str) -> str:
    """Where `push` sends tags and `scan` reads them. CI points this at
    git@github.com:<drop>.git (its deploy key); the publisher uses the
    public https URL, unauthenticated."""
    return (os.environ.get("BURROW_MIRROR_DROP_GIT")
            or f"https://github.com/{_drop_repo(repo)}.git")


_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def _git_anon(args: list[str]) -> subprocess.CompletedProcess:
    """git against the public drop repo with NO credentials: no token in the
    environment, no credential helper, no prompt. The publisher never hands
    the drop repo anything it could use."""
    env = {k: v for k, v in os.environ.items() if k not in _TOKEN_VARS}
    env["GIT_TERMINAL_PROMPT"] = "0"
    p = subprocess.run(["git", "-c", "credential.helper=", *args], capture_output=True,
                       text=True, stdin=subprocess.DEVNULL, env=env)
    if p.returncode != 0:
        raise Refuse(f"`git {' '.join(args)}` failed: {p.stderr.strip() or p.stdout.strip()}")
    return p


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


def _api_get(path: str):
    """GET a GitHub API path: gh's token first, unauthenticated (public repo)
    if that is refused. None on any failure (callers poll)."""
    p = _run(["api", path], check=False)
    if p.returncode == 0:
        try:
            return json.loads(p.stdout)
        except ValueError:
            pass
    api = os.environ.get("BURROW_MIRROR_API", "https://api.github.com")
    req = urllib.request.Request(f"{api}/{path}",
                                 headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001 — transient; the caller keeps polling
        return None


def _result_path(sha: str) -> str:
    if not _SHA.match(sha):
        raise Refuse(f"{sha!r} is not a commit sha")
    return f"{RESULTS_DIR}/{sha}.json"


def _mirror_result(repo: str, sha: str) -> dict | None:
    """The publisher's result for drop-repo tag commit SHA, read from
    results/<sha>.json on the MIRROR's results branch (public; no token
    needed). None if there is none (yet)."""
    got = _api_get(f"repos/{repo}/contents/{_result_path(sha)}?ref={RESULTS_BRANCH}")
    if not isinstance(got, dict) or "content" not in got:
        return None
    try:
        r = json.loads(base64.b64decode(got["content"]))
    except ValueError:
        return None
    if not isinstance(r, dict) or r.get("commit") != sha or r.get("context") != STATUS_CONTEXT:
        return None
    return r


def _wait_for_mirror(repo: str, sha: str, ref: str) -> None:
    """Block until the mirror's publisher (a scheduled run from main) has
    processed tag commit SHA; Refuse unless it succeeded. Scheduled runs can
    start late, hence the long default."""
    timeout = float(os.environ.get("BURROW_MIRROR_WAIT", "3600"))
    poll = float(os.environ.get("BURROW_MIRROR_POLL", "30"))
    deadline = time.monotonic() + timeout
    while True:
        st = _mirror_result(repo, sha)
        if st and st.get("state") in ("success", "failure", "error"):
            if st["state"] != "success":
                raise Refuse(f"the mirror's publisher refused {ref}: "
                             f"{st.get('description', '')} {st.get('target_url', '')}".rstrip())
            print(f"   mirror publisher processed {ref}")
            return
        if time.monotonic() >= deadline:
            raise Refuse(f"the mirror's publisher didn't process {ref} within {int(timeout)}s "
                         "(it runs on a schedule from the mirror's main)")
        time.sleep(poll)


def _asset_commit(version: str, d: pathlib.Path, names: list[str], phase: str,
                  work: pathlib.Path) -> str:
    """A ROOT commit holding exactly assets/NAMES from DIR — data only: no
    workflow, no script (the publisher's code comes from mirror main). → sha"""
    _git(["init", "-q", str(work)])
    (work / ASSET_DIR).mkdir()
    for n in names:
        shutil.copyfile(d / n, work / ASSET_DIR / n)
    _git(["add", "-A"], cwd=work)
    _git(["-c", "user.name=burrowai-dev release", "-c", "user.email=releases@burrowai.dev",
          "-c", "commit.gpgsign=false", "commit", "-q",
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
        sha = _asset_commit(version, d, names, phase, work)
        # Every push is a fresh commit (new timestamp) → a new sha the
        # publisher hasn't seen, so a re-run is always re-processed.
        _git(["push", "-q", "--force", _drop_url(repo), f"HEAD:refs/tags/{ref}"], cwd=work)
    print(f"   pushed {ref} → {sha[:12]} ({len(names)} assets) to the drop repo "
          f"{_drop_repo(repo)}; waiting for the mirror's publisher")
    # Best effort: start a publisher run now instead of at the next 5-minute
    # tick. Works with Sam's own gh login (promote_release.sh); CI's token
    # can't dispatch another repo's workflow, so CI waits for the schedule.
    _run(["workflow", "run", "publish.yml", "--repo", repo, "--ref", "main"], check=False)
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
            time.sleep(float(os.environ.get("BURROW_MIRROR_POLL", "30")))
    raise Refuse(f"GitHub release {tag} was published but its public copy doesn't serve "
                 "every asset yet")


# ─── the mirror's publisher (publish.yml → scan, code from main) ─────

def _marker(sha: str) -> str:
    return f"<!-- mirror-processed {sha} -->"


def _list_releases(repo: str) -> dict[str, dict]:
    p = _run(["api", "--paginate", f"repos/{repo}/releases?per_page=100",
              "--jq", ".[] | {tag_name, draft, body}"])
    out = {}
    for line in p.stdout.splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["tag_name"]] = r
    return out


def _release_tags(url: str) -> list[tuple[str, str, str]]:
    """[(tag, version, commit sha)] for every v<semver> / publish/v<semver>
    tag on the drop repo, oldest version first, each v<ver> before its
    publish/v<ver>. Other tags are ignored. Unauthenticated."""
    p = _git_anon(["ls-remote", "--tags", url])
    refs: dict[str, str] = {}
    for line in p.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/"):]
        if name.endswith("^{}"):
            refs[name[:-3]] = sha                  # annotated: the commit
        else:
            refs.setdefault(name, sha)
    out = []
    for name, sha in refs.items():
        m = _TAG.match(name)
        if m:
            out.append((name, m.group(2), sha))
        else:
            print(f"   ignoring tag {name!r} (not v<semver> / publish/v<semver>)")
    out.sort(key=lambda x: (_semver(x[1]), x[0].startswith(PUBLISH_TAG_PREFIX)))
    return out


def _extract_assets(url: str, tag: str, dest: pathlib.Path, git_dir: pathlib.Path) -> str:
    """Fetch tag TAG into a scratch bare repo and write ONLY its assets/*
    regular files into DEST, as data. Nothing else in the commit is looked
    at, let alone run. → the commit sha that was read."""
    if not git_dir.exists():
        _git(["init", "-q", "--bare", str(git_dir)])
    _git_anon(["--git-dir", str(git_dir), "fetch", "-q", "--no-tags", "--depth", "1", url,
               f"+refs/tags/{tag}:refs/scan/x"])
    sha = _git(["--git-dir", str(git_dir), "rev-parse", "refs/scan/x^{commit}"]).stdout.strip()
    top = _git(["--git-dir", str(git_dir), "ls-tree", "--name-only", "-z", sha]).stdout
    extra = sorted(n for n in top.split("\0") if n and n != ASSET_DIR)
    if extra:
        # An upload is assets/ and nothing else. Anything more (a
        # .github/workflows file above all) is not ours: refuse it loudly.
        raise Refuse(f"{tag} holds more than {ASSET_DIR}/ ({', '.join(extra)}) — not an "
                     "upload from the release pipeline")
    ls = _git(["--git-dir", str(git_dir), "ls-tree", "-z", sha, f"{ASSET_DIR}/"]).stdout
    dest.mkdir(parents=True, exist_ok=True)
    for entry in filter(None, ls.split("\0")):
        meta, _, path = entry.partition("\t")
        mode, kind, obj = meta.split()
        name = path[len(ASSET_DIR) + 1:]
        if mode != "100644" or kind != "blob" or not _SAFE_NAME.match(name):
            print(f"   {tag}: skipping {path!r} (not a plain asset file)")
            continue
        blob = subprocess.run(["git", "--git-dir", str(git_dir), "cat-file", "blob", obj],
                              capture_output=True, check=True).stdout
        (dest / name).write_bytes(blob)
    return sha


def _api_json(args: list[str], body: dict | None = None, *, check: bool = True):
    """gh api with a JSON body (--input). → (returncode, parsed stdout or None)"""
    f = None
    if body is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(body, f)
        args = [*args, "--input", f.name]
    try:
        p = _run(["api", *args], check=check)
    finally:
        if f is not None:
            os.unlink(f.name)
    try:
        return p.returncode, json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        return p.returncode, None


def _ensure_results_branch(repo: str) -> None:
    """Create the mirror's results branch (an orphan commit holding only a
    README: no workflow) if it doesn't exist yet."""
    rc, _ = _api_json([f"repos/{repo}/git/ref/heads/{RESULTS_BRANCH}"], check=False)
    if rc == 0:
        return
    readme = ("Results of the mirror's publisher (publish.yml on main): one "
              "results/<tag commit sha>.json per processed upload tag of the drop repo.\n")
    _, tree = _api_json(["-X", "POST", f"repos/{repo}/git/trees"], {"tree": [
        {"path": "README.md", "mode": "100644", "type": "blob", "content": readme}]})
    _, commit = _api_json(["-X", "POST", f"repos/{repo}/git/commits"],
                          {"message": "results branch", "tree": tree["sha"], "parents": []})
    _api_json(["-X", "POST", f"repos/{repo}/git/refs"],
              {"ref": f"refs/heads/{RESULTS_BRANCH}", "sha": commit["sha"]})


def _record_result(repo: str, tag: str, sha: str, state: str, desc: str) -> None:
    """Write results/<sha>.json on the mirror's results branch with this run's
    GITHUB_TOKEN (the drop repo's deploy key can't write the mirror)."""
    path = _result_path(sha)
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
                   f"/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    doc = {"context": STATUS_CONTEXT, "drop": _drop_repo(repo), "tag": tag, "commit": sha,
           "state": state, "description": desc[:1000], "target_url": run_url}
    _ensure_results_branch(repo)
    body = {"message": f"{tag} {sha[:12]}: {state}", "branch": RESULTS_BRANCH,
            "content": base64.b64encode((json.dumps(doc, indent=2, sort_keys=True)
                                         + "\n").encode()).decode()}
    rc, cur = _api_json([f"repos/{repo}/contents/{path}?ref={RESULTS_BRANCH}"], check=False)
    if rc == 0 and isinstance(cur, dict) and "sha" in cur:
        body["sha"] = cur["sha"]
    _api_json(["-X", "PUT", f"repos/{repo}/contents/{path}"], body)


def _note_processed(repo: str, tag: str, sha: str) -> None:
    """Record SHA in release TAG's notes so later scans skip it without an
    API call per tag."""
    rel = _list_releases(repo).get(tag)
    if rel is None:
        return
    body = rel.get("body") or ""
    if _marker(sha) not in body:
        _run(["release", "edit", tag, "--repo", repo, "--notes", f"{body}\n{_marker(sha)}"])


def scan(repo: str, keys_path: pathlib.Path) -> int:
    """One publisher pass. Runs ONLY from the mirror's protected main
    (publish.yml on schedule / workflow_dispatch). For each release tag
    commit not processed yet: read its assets as data, verify_release
    against the key table on main, then draft (v<ver>) or publish
    (publish/v<ver>), and record the outcome in the mirror's results
    branch. Tags come from the drop repo; nothing but assets/* is read."""
    try:
        table = parse_table(keys_path.read_bytes())
    except OSError as e:
        raise Refuse(f"{keys_path}: can't read the key table ({e})") from None
    url = _drop_url(repo)
    rels = _list_releases(repo)
    refused = 0
    with tempfile.TemporaryDirectory() as tmp:
        git_dir = pathlib.Path(tmp) / "tags.git"
        for i, (tag, version, sha) in enumerate(_release_tags(url)):
            rel_tag = f"v{version}"
            rel = rels.get(rel_tag)
            if rel is not None and _marker(sha) in (rel.get("body") or ""):
                continue
            if not _SHA.match(sha):
                print(f"   ignoring tag {tag!r} (not a sha-1 commit)")
                continue
            if rel is None and _mirror_result(repo, sha):
                continue                       # refused before any release existed
            d = pathlib.Path(tmp) / f"t{i}"
            try:
                sha = _extract_assets(url, tag, d, git_dir)
                notes = verify_release(version, d, table)
                if tag.startswith(PUBLISH_TAG_PREFIX):
                    finalize(version, d, repo)
                else:
                    # The tag lives in the drop repo; the mirror's own tag
                    # v<ver> is made by GitHub on publish, at mirror main.
                    draft(version, d, repo, target="main", verify_tag=False,
                          notes_extra=f"{notes}\n\nUploaded as {_drop_repo(repo)}@{sha}.")
                _record_result(repo, tag, sha, "success", f"{tag}: done")
                print(f"✓ {tag} ({sha[:12]})")
            except Refuse as e:
                refused += 1
                print(f"✗ {tag} ({sha[:12]}): {e}", file=sys.stderr)
                try:
                    _record_result(repo, tag, sha, "failure", f"{tag}: {e}")
                except Refuse as e2:            # e.g. a tag that isn't a commit
                    print(f"   (couldn't record the refusal: {e2})", file=sys.stderr)
            _note_processed(repo, rel_tag, sha)
            rels = _list_releases(repo)
    # A refusal fails this run (GitHub notifies), once: the tag is recorded.
    return 1 if refused else 0


# ─── admin path: mirror main (Sam's own gh login, repository admin) ──

def _find_ruleset(repo: str) -> dict | None:
    p = _run(["api", f"repos/{repo}/rulesets?per_page=100"])
    for r in json.loads(p.stdout or "[]"):
        if r.get("name") == RULESET_NAME:
            return json.loads(_run(["api", f"repos/{repo}/rulesets/{r['id']}"]).stdout)
    return None


def _put_ruleset(repo: str, rid: int, body: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(body, f)
    try:
        return json.loads(_run(["api", "-X", "PUT", f"repos/{repo}/rulesets/{rid}",
                                "--input", f.name]).stdout)
    finally:
        os.unlink(f.name)


def _locked(rs: dict) -> bool:
    return (rs.get("enforcement") == "active" and not rs.get("bypass_actors")
            and {r.get("type") for r in rs.get("rules") or []}
            >= {"update", "deletion", "non_fast_forward"}
            and "~DEFAULT_BRANCH" in ((rs.get("conditions") or {}).get("ref_name") or {})
            .get("include", []))


def protect_mirror(repo: str) -> int:
    """Create (or re-assert) the ruleset that makes mirror main unwritable:
    update, deletion and force-push blocked, no bypass actors."""
    rs = _find_ruleset(repo)
    if rs is None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(RULESET, f)
        try:
            rs = json.loads(_run(["api", "-X", "POST", f"repos/{repo}/rulesets",
                                  "--input", f.name]).stdout)
        finally:
            os.unlink(f.name)
    elif not _locked(rs):
        rs = _put_ruleset(repo, rs["id"], RULESET)
    rs = _find_ruleset(repo)
    if rs is None or not _locked(rs):
        raise Refuse(f"ruleset {RULESET_NAME} on {repo} isn't in force")
    print(f"✓ {repo} main protected by ruleset {rs['id']} ({RULESET_NAME})")
    return 0


def check_setup(repo: str) -> int:
    """Verify the mirror's trust setup (read-only): the drop repo is public
    with Actions DISABLED and exactly one write deploy key (CI's); the mirror
    has NO deploy key; the ruleset protecting mirror main is in force."""
    drop = _drop_repo(repo)
    bad: list[str] = []

    def get(path):
        rc, got = _api_json([path], check=False)
        return got if rc == 0 else None

    info = get(f"repos/{drop}")
    if not isinstance(info, dict) or info.get("private") is not False:
        bad.append(f"drop repo {drop} isn't a public repo")
    perms = get(f"repos/{drop}/actions/permissions")
    if not isinstance(perms, dict) or perms.get("enabled") is not False:
        bad.append(f"GitHub Actions is NOT disabled on {drop} (actions/permissions "
                   f"enabled={None if not isinstance(perms, dict) else perms.get('enabled')})")
    keys = get(f"repos/{drop}/keys")
    if not isinstance(keys, list) or len(keys) != 1 or keys[0].get("read_only") is not False:
        bad.append(f"{drop} should have exactly one write deploy key (CI's), has "
                   f"{keys if not isinstance(keys, list) else len(keys)}")
    mkeys = get(f"repos/{repo}/keys")
    if mkeys != []:
        bad.append(f"the mirror {repo} must have NO deploy keys, has "
                   f"{mkeys if not isinstance(mkeys, list) else len(mkeys)}")
    try:
        rs = _find_ruleset(repo)
    except Refuse as e:
        rs, bad = None, bad + [str(e)]
    if rs is None or not _locked(rs):
        bad.append(f"ruleset {RULESET_NAME} on {repo} isn't in force with no bypass")
    for b in bad:
        print(f"✗ {b}", file=sys.stderr)
    if bad:
        return 1
    print(f"✓ {drop}: public, Actions disabled, one write deploy key; {repo}: no deploy "
          f"keys, main locked by ruleset {rs['id']}")
    return 0


def _main_head(repo: str) -> str:
    out = _git(["ls-remote", _git_url(repo), "refs/heads/main"]).stdout.split()
    return out[0] if out else ""


def _admin_write_main(repo: str, files: dict[str, bytes], message: str) -> list[str]:
    """Commit FILES to mirror main through the contents API: the ruleset
    grants the repository-admin role a bypass for the write and loses it
    again right after (always, and verified). The deploy key and Actions
    are never bypass actors. → the commit shas, in order."""
    rs = _find_ruleset(repo)
    if rs is None or not _locked(rs):
        raise Refuse(f"mirror main isn't protected (ruleset {RULESET_NAME}) — run "
                     "`protect-mirror` first")
    shas: list[str] = []
    _put_ruleset(repo, rs["id"], {"bypass_actors": [ADMIN_BYPASS]})
    try:
        for path, data in files.items():
            cur = _run(["api", f"repos/{repo}/contents/{path}?ref=main"], check=False)
            body = {"message": message, "branch": "main",
                    "content": base64.b64encode(data).decode()}
            if cur.returncode == 0:
                body["sha"] = json.loads(cur.stdout)["sha"]
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(body, f)
            try:
                res = _run(["api", "-X", "PUT", f"repos/{repo}/contents/{path}",
                            "--input", f.name])
            finally:
                os.unlink(f.name)
            shas.append(json.loads(res.stdout)["commit"]["sha"])
    finally:
        _put_ruleset(repo, rs["id"], {"bypass_actors": []})
        after = _find_ruleset(repo)
        if after is None or not _locked(after):
            raise Refuse(f"ruleset {RULESET_NAME} did NOT go back to no-bypass — fix it now "
                         "(`protect-mirror`)")
    head = _main_head(repo)
    if shas and head != shas[-1]:
        raise Refuse(f"mirror main is at {head[:12]}, not the commit just written "
                     f"({shas[-1][:12]}) — something else wrote main during the window")
    return shas


def _main_file(repo: str, path: str) -> bytes | None:
    got = _api_get(f"repos/{repo}/contents/{path}?ref=main")
    if not isinstance(got, dict) or "content" not in got:
        return None
    return base64.b64decode(got["content"])


def sync_keys(version: str, d: pathlib.Path, repo: str) -> int:
    """Make the key table on mirror main the one the release in DIR pins
    (first signed release, or a key rotation); signed_since is set once, to
    the first version that needed it. No-op if it already matches. Run by
    promote_release.sh before the draft push."""
    assets(version, d)
    wheel = d / _manifest_files(version, d)[0][0]
    want = pinned_keys(wheel)
    if not want:
        raise Refuse(f"{wheel.name} pins no release keys — nothing to put in {KEY_TABLE}")
    m = json.loads((d / "latest.json").read_bytes())
    if (d / "latest.json.sig").is_file() and m.get("key_id") not in want:
        raise Refuse(f"{version} is signed by {m.get('key_id')!r}, which its own key table "
                     "doesn't contain")
    raw = _main_file(repo, KEY_TABLE)
    if raw is None:
        raise Refuse(f"can't read {KEY_TABLE} from {repo} main")
    cur = parse_table(raw)
    if cur["keys"] == want:
        print(f"✓ {KEY_TABLE} on {repo} main already pins these keys")
        return 0
    new = {"product": PRODUCT, "keys": want, "signed_since": cur["signed_since"] or version}
    data = (json.dumps(new, indent=2, sort_keys=True) + "\n").encode()
    _admin_write_main(repo, {KEY_TABLE: data}, f"release keys for {version} "
                      f"(fingerprint {fingerprint(want)})")
    print(f"✓ {KEY_TABLE} on {repo} main: {fingerprint(want)} (signed since "
          f"{new['signed_since']})")
    return 0


def update_publisher(repo: str) -> int:
    """Put this script + scripts/mirror/publish.yml on mirror main (the
    publisher's code). Unchanged files are skipped."""
    files = {}
    for src, dst in ((pathlib.Path(__file__).resolve(), MIRROR_SCRIPT),
                     (HERE / "mirror" / "publish.yml", MIRROR_WORKFLOW)):
        data = src.read_bytes()
        if _main_file(repo, dst) != data:
            files[dst] = data
    if not files:
        print(f"✓ the publisher on {repo} main is current")
        return 0
    _admin_write_main(repo, files, "publisher: update from burrowai-dev")
    print(f"✓ updated {', '.join(files)} on {repo} main")
    return 0


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
        keys = _opt(argv, "--keys", str(HERE.parent.parent / KEY_TABLE))
        if argv == ["scan"]:
            return scan(repo, pathlib.Path(keys))
        if len(argv) == 3 and argv[0] == "sync-keys":
            return sync_keys(argv[1], pathlib.Path(argv[2]), repo)
        if argv == ["update-publisher"]:
            return update_publisher(repo)
        if argv == ["protect-mirror"]:
            return protect_mirror(repo)
        if argv == ["check-setup"]:
            return check_setup(repo)
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
