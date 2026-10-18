#!/usr/bin/env python3
"""Detect new versions with nvchecker and update the PKGBUILDs.

For each package (a top-level directory with a PKGBUILD and .nvchecker.toml):

  1. Run nvchecker and read the latest version it reports.
  2. Compare it with the PKGBUILD's current pkgver (using pacman's vercmp).
  3. If newer, bump pkgver/pkgrel, regenerate checksums (updpkgsums) and the
     .SRCINFO (makepkg --printsrcinfo).
  4. If the package needs custom logic (VCS packages with pkgver(), etc.), run
     <package>/.ci/update.sh instead, with NEWVER and OLDPKGVER in the
     environment; that script does the whole bump itself.

Errors in one package don't abort the rest: they are reported as warnings and
in the run summary. The script does not commit; the workflow does. It exits
non-zero only when every checked package fails.

The AUR commit message is "upgpkg: <pkg> <ver>-<rel>"; publish.yml derives the
same format from the PKGBUILD, so keep both in sync.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Values for the "action" field of each package in the summary.
SKIPPED = "skipped"
NO_VERSION = "no version found"
UP_TO_DATE = "up to date"
WOULD_UPDATE = "would update"
UPDATED = "updated"
ERROR = "error"

SUMS_RE = re.compile(
    r"^(?:ck|md5|sha1|sha224|sha256|sha384|sha512|b2)sums(?:_[A-Za-z0-9_]+)?=", re.M
)
PKGVER_FN_RE = re.compile(r"^pkgver\(\)", re.M)


def run(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True)


def pkg_vars(pkgdir: Path) -> dict[str, str]:
    """Read pkgver/pkgrel from the PKGBUILD (sourced in bash, like makepkg does)."""
    out = run(
        ["bash", "-c", 'source ./PKGBUILD; printf "%s\\n%s\\n" "$pkgver" "$pkgrel"'],
        cwd=pkgdir,
    ).stdout
    pkgver, pkgrel = out.splitlines()
    return {"pkgver": pkgver, "pkgrel": pkgrel}


def vercmp(a: str, b: str) -> int:
    """Compare versions the pacman way: <0 if a<b, 0 if equal, >0 if a>b.

    Both sides are compared bare: the PKGBUILD epoch (if any) applies to the
    current pkgver and to whatever comes next, so it cancels out.
    """
    return int(run(["vercmp", a, b]).stdout.strip())


def latest_upstream(pkgdir: Path) -> str | None:
    """Run nvchecker on the package's .nvchecker.toml and return the version.

    With --logger json nvchecker writes one JSON event per line to stdout.
    """
    out = run(["nvchecker", "-c", ".nvchecker.toml", "--logger", "json"], cwd=pkgdir).stdout
    versions: dict[str, str] = {}
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("name") and event.get("version"):
            versions[event["name"]] = event["version"]

    unique = set(versions.values())
    if len(unique) == 1:
        return unique.pop()
    if pkgdir.name in versions:
        return versions[pkgdir.name]
    return None


def bump(pkgdir: Path, newver: str, old_pkgver: str) -> None:
    """Update the package to newver, the package's own way or the default one."""
    override = pkgdir / ".ci" / "update.sh"
    if override.exists():
        env = dict(os.environ, NEWVER=newver, OLDPKGVER=old_pkgver)
        run(["bash", str(override)], cwd=pkgdir, env=env)
    else:
        pkgbuild = pkgdir / "PKGBUILD"
        text = pkgbuild.read_text()
        if PKGVER_FN_RE.search(text):
            raise RuntimeError("PKGBUILD uses pkgver() (VCS package?): add .ci/update.sh")
        text, n = re.subn(r"^pkgver=.*$", f"pkgver={newver}", text, count=1, flags=re.M)
        if n != 1:
            raise RuntimeError("no pkgver= line found in PKGBUILD")
        text = re.sub(r"^pkgrel=.*$", "pkgrel=1", text, count=1, flags=re.M)
        pkgbuild.write_text(text)
        if SUMS_RE.search(text):
            # updpkgsums is architecture-independent: makepkg -g downloads the
            # sources of every arch in arch[] and rewrites all sums arrays.
            # SRCDEST keeps those downloads out of the package directory.
            with tempfile.TemporaryDirectory(prefix="updpkgsums-srcdest-") as srcdest:
                run(["updpkgsums"], cwd=pkgdir, env=dict(os.environ, SRCDEST=srcdest))

    srcinfo = run(["makepkg", "--printsrcinfo"], cwd=pkgdir).stdout
    (pkgdir / ".SRCINFO").write_text(srcinfo)


def rollback(pkgdir: Path) -> None:
    """Undo a failed bump: restore tracked files, then drop newly created ones."""
    name = pkgdir.name
    is_tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", name], cwd=ROOT, capture_output=True
    ).returncode == 0
    cmds = [["git", "restore", "--", name]]
    if is_tracked:
        # git clean would also delete an uncommitted new package directory.
        cmds.append(["git", "clean", "-fdq", "--", name])
    for cmd in cmds:
        try:
            run(cmd, cwd=ROOT)
        except subprocess.CalledProcessError as exc:
            print(f"::warning::rollback failed: {' '.join(cmd)}: {exc.stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="only report, change nothing")
    parser.add_argument("--summary", type=Path, help="file to write the JSON summary to")
    args = parser.parse_args()

    results: list[dict] = []
    changed: list[dict] = []

    for pkgbuild in sorted(ROOT.glob("*/PKGBUILD")):
        pkgdir = pkgbuild.parent
        entry: dict = {"dir": pkgdir.name}
        try:
            if not (pkgdir / ".nvchecker.toml").exists():
                entry.update(action=SKIPPED, detail="no .nvchecker.toml")
            elif (upstream := latest_upstream(pkgdir)) is None:
                entry.update(action=NO_VERSION, detail="nvchecker found no version")
            else:
                entry["upstream"] = upstream
                current = pkg_vars(pkgdir)
                if vercmp(upstream, current["pkgver"]) > 0:
                    if args.dry_run:
                        entry["action"] = WOULD_UPDATE
                    else:
                        try:
                            bump(pkgdir, upstream, current["pkgver"])
                        except Exception:
                            rollback(pkgdir)
                            raise
                        new = pkg_vars(pkgdir)
                        version = f"{new['pkgver']}-{new['pkgrel']}"
                        entry.update(action=UPDATED, newver=version)
                        changed.append({"dir": pkgdir.name, "version": version})
                else:
                    entry["action"] = UP_TO_DATE
        except Exception as exc:
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr.strip():
                detail = exc.stderr.strip()
            else:
                detail = str(exc)
            lines = detail.splitlines()
            entry.update(action=ERROR, detail=lines[0].strip() if lines else exc.__class__.__name__)
        results.append(entry)

    message = ""
    if changed:
        message = "upgpkg: " + ", ".join(f"{c['dir']} {c['version']}" for c in changed)

    summary = {"changed": changed, "results": results, "message": message}
    if args.summary:
        args.summary.write_text(json.dumps(summary, indent=2) + "\n")

    def detail_of(entry: dict) -> str:
        return entry.get("detail") or entry.get("newver") or entry.get("upstream") or ""

    for entry in results:
        note = detail_of(entry)
        suffix = f" ({note})" if note else ""
        print(f"{entry['dir']}: {entry['action']}{suffix}")
    for entry in results:
        if entry["action"] == ERROR:
            print(f"::warning title={entry['dir']}::{entry.get('detail', 'error')}")

    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary:
        try:
            lines = ["## nvchecker result", "", "| Package | Status | Detail |", "|---|---|---|"]
            for entry in results:
                lines.append(f"| `{entry['dir']}` | {entry['action']} | {detail_of(entry)} |")
            if changed:
                names = ", ".join(f"{c['dir']} {c['version']}" for c in changed)
                lines += ["", f"**Publishing:** {names}"]
            with open(gh_summary, "a") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    checked = [entry for entry in results if entry["action"] != SKIPPED]
    if checked and all(entry["action"] == ERROR for entry in checked):
        print("::error::all checked packages failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
