#!/usr/bin/env python3
"""Update in temporary copies, prepare AUR trees, or publish prepared files."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAME = re.compile(r"[a-z0-9@_+][a-z0-9@._+-]*")
# A conservative, shell-safe subset of Arch's pkgver syntax.
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+]*")
SUMS_RE = re.compile(
    r"^(?:ck|md5|sha1|sha224|sha256|sha384|sha512|b2)sums(?:_[A-Za-z0-9_]+)?=", re.M
)


def run(cmd: list[str], cwd: Path, **kwargs) -> str:
    return subprocess.run(
        cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, text=True, **kwargs
    ).stdout


def packages() -> list[Path]:
    # A missing exclusion file is an error, not permission to publish everything.
    excluded = {
        name
        for line in (ROOT / ".github/excluded-packages").read_text().splitlines()
        if (name := line.partition("#")[0].strip())
    }
    if any(not NAME.fullmatch(name) for name in excluded):
        raise ValueError("invalid name in excluded-packages")
    result = []
    for pkgbuild in sorted(ROOT.glob("*/PKGBUILD")):
        pkgdir = pkgbuild.parent
        if pkgdir.name.startswith("."):
            continue
        if not NAME.fullmatch(pkgdir.name) or pkgdir.is_symlink():
            raise ValueError(f"invalid package directory: {pkgdir.name!r}")
        if pkgdir.name not in excluded:
            result.append(pkgdir)
    return result


def copy_package(pkgdir: Path, dest: Path) -> None:
    """Copy only tracked, flat AUR assets; never publish local build artifacts."""
    dest.mkdir()
    tracked = run(["git", "ls-files", "-z", "--", f"{pkgdir.name}/"], ROOT)
    for name in tracked.split("\0"):
        if not name:
            continue
        relative = Path(name).relative_to(pkgdir.name)
        if any(part.startswith(".") for part in relative.parts) and relative != Path(".SRCINFO"):
            continue
        source = ROOT / name
        if len(relative.parts) != 1 or source.is_symlink() or not source.is_file():
            raise ValueError(f"AUR assets must be regular, top-level files: {name!r}")
        shutil.copy2(source, dest / relative)


def metadata(srcinfo: str, name: str) -> dict[str, str]:
    values = dict(
        line.strip().split(" = ", 1)
        for line in srcinfo.splitlines()
        if line.strip().partition(" = ")[0] in {"pkgbase", "pkgver", "pkgrel", "epoch"}
    )
    if values.get("pkgbase") != name or not VERSION.fullmatch(values.get("pkgver", "")):
        raise ValueError(f"invalid pkgbase/pkgver for {name}")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", values.get("pkgrel", "")):
        raise ValueError(f"invalid pkgrel for {name}")
    if "epoch" in values and not re.fullmatch(r"[0-9]+", values["epoch"]):
        raise ValueError(f"invalid epoch for {name}")
    return values


def commit_message(name: str, values: dict[str, str]) -> str:
    version = f"{values['pkgver']}-{values['pkgrel']}"
    if "epoch" in values:
        version = f"{values['epoch']}:{version}"
    return f"upgpkg: {name} {version}"


def update_commit_message() -> None:
    changed = set(run(["git", "diff", "--cached", "--name-only", "-z"], ROOT).split("\0"))
    for pkgdir in packages():
        if any(f"{pkgdir.name}/{name}" in changed for name in ("PKGBUILD", ".SRCINFO")):
            values = metadata((pkgdir / ".SRCINFO").read_text(), pkgdir.name)
            print(commit_message(pkgdir.name, values))


def latest_upstream(pkgdir: Path) -> str:
    config = tomllib.loads((pkgdir / ".nvchecker.toml").read_text())
    expected = set(config) - {"__config__"}
    versions = {}
    output = run(["nvchecker", "-c", ".nvchecker.toml", "--logger", "json"], pkgdir)
    for line in output.splitlines():
        event = json.loads(line)
        if event.get("level") == "error" or event.get("event") == "no-result":
            raise ValueError(f"nvchecker failed for {pkgdir.name}")
        if event.get("event") in {"updated", "up-to-date"}:
            version = event.get("version")
            if not isinstance(version, str) or not VERSION.fullmatch(version):
                raise ValueError(f"unsafe upstream version: {version!r}")
            versions[event["name"]] = version
    if not expected or set(versions) != expected or len(set(versions.values())) != 1:
        raise ValueError(f"missing or inconsistent upstream versions for {pkgdir.name}")
    return next(iter(versions.values()))


def update(dry_run: bool) -> None:
    changed = []
    with tempfile.TemporaryDirectory(prefix="aur-update-") as temp:
        for pkgdir in packages():
            if not (pkgdir / ".nvchecker.toml").is_file():
                continue
            dest = Path(temp) / pkgdir.name
            copy_package(pkgdir, dest)
            shutil.copyfile(pkgdir / ".nvchecker.toml", dest / ".nvchecker.toml")
            newver = latest_upstream(dest)
            current = metadata(run(["makepkg", "--printsrcinfo"], dest), pkgdir.name)
            if int(run(["vercmp", newver, current["pkgver"]], dest)) <= 0:
                print(f"{pkgdir.name}: up to date")
                continue
            if (pkgdir / ".ci/update.sh").exists():
                raise ValueError("custom .ci/update.sh is not supported; exclude the package or update it manually")
            print(f"{pkgdir.name}: {current['pkgver']} -> {newver}")
            if dry_run:
                continue
            pkgbuild = dest / "PKGBUILD"
            text = pkgbuild.read_text()
            if re.search(r"(?m)^\s*(?:function\s+pkgver\b|pkgver\s*\(\s*\))", text):
                raise ValueError("automatic updates do not support pkgver()")
            for key, value in (("pkgver", newver), ("pkgrel", "1")):
                text, count = re.subn(rf"(?m)^{key}=.*$", f"{key}={value}", text)
                if count != 1:
                    raise ValueError(f"expected exactly one {key}= assignment")
            pkgbuild.write_text(text)
            if SUMS_RE.search(text):
                sources = dest / "sources"
                sources.mkdir()
                # makepkg -g (used by updpkgsums) downloads and hashes all architectures.
                run(["updpkgsums"], dest, env=dict(os.environ, SRCDEST=str(sources)))
            srcinfo = run(["makepkg", "--printsrcinfo"], dest)
            values = metadata(srcinfo, pkgdir.name)
            if values["pkgver"] != newver or values["pkgrel"] != "1":
                raise ValueError("PKGBUILD did not retain the requested version")
            (dest / ".SRCINFO").write_text(srcinfo)
            changed.append((pkgdir, dest))
        # No original files change until every package has succeeded.
        for pkgdir, dest in changed:
            for name in ("PKGBUILD", ".SRCINFO"):
                shutil.copyfile(dest / name, pkgdir / name)


def prepare(staging: Path) -> None:
    staging.mkdir(exist_ok=True)
    for pkgdir in packages():
        dest = staging / pkgdir.name
        copy_package(pkgdir, dest)
        srcinfo = run(["makepkg", "--printsrcinfo"], dest)
        metadata(srcinfo, pkgdir.name)
        if srcinfo != (dest / ".SRCINFO").read_text():
            raise ValueError(f"regenerate {pkgdir.name}/.SRCINFO with makepkg --printsrcinfo")


def publish(staging: Path) -> None:
    """Publish prepared files. This function does not execute PKGBUILDs.

    The prepare step in this job runs package code before the AUR secrets
    are injected. Merged package code is trusted; this is not a sandbox.
    """
    identity = {key: os.environ.pop(key) for key in ("AUR_USERNAME", "AUR_EMAIL", "AUR_SSH_PRIVATE_KEY")}
    if not all(value.strip() for value in identity.values()):
        raise ValueError("AUR credentials must not be empty")
    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="aur-publish-") as temp:
        key = Path(temp) / "key"
        key.write_text(identity["AUR_SSH_PRIVATE_KEY"].rstrip("\n") + "\n")
        key.chmod(0o600)
        env["GIT_SSH_COMMAND"] = shlex.join([
            "ssh", "-F", "/dev/null", "-i", str(key), "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15",
            "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "HostKeyAlgorithms=ssh-ed25519",
            "-o", f"UserKnownHostsFile={ROOT / '.github/aur-known-hosts'}",
        ])
        # AUR pushes are per package, not atomic across packages. A later
        # failure can leave earlier packages published.
        for pkgdir in packages():
            source = staging / pkgdir.name
            values = metadata((source / ".SRCINFO").read_text(), pkgdir.name)
            # Refuse a stale checkout; the next queued run reconciles current main.
            head = run(["git", "rev-parse", "HEAD"], ROOT).strip()
            remote = run(["git", "ls-remote", "--exit-code", "origin", "refs/heads/main"], ROOT)
            if remote.split()[0] != head:
                raise ValueError("main changed during preparation; rerun the workflow")
            repo = Path(temp) / pkgdir.name
            run(["git", "clone", f"ssh://aur@aur.archlinux.org/{pkgdir.name}.git", str(repo)], ROOT, env=env)
            run(["git", "rm", "-r", "--ignore-unmatch", "--", "."], repo)
            for asset in source.iterdir():
                if not asset.is_file() or asset.is_symlink():
                    raise ValueError(f"invalid staged asset: {asset.name!r}")
                shutil.copy2(asset, repo / asset.name)
            run(["git", "add", "--all"], repo)
            diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo)
            if diff.returncode == 0:
                print(f"{pkgdir.name}: AUR is up to date")
                continue
            if diff.returncode != 1:
                diff.check_returncode()
            run(["git", "-c", f"user.name={identity['AUR_USERNAME']}",
                 "-c", f"user.email={identity['AUR_EMAIL']}", "commit", "-m",
                 commit_message(pkgdir.name, values)], repo)
            run(["git", "push", "origin", "HEAD:master"], repo, env=env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("update").add_argument("--dry-run", action="store_true")
    commands.add_parser("commit-message")
    for command in ("prepare", "publish"):
        commands.add_parser(command).add_argument("staging", type=Path)
    args = parser.parse_args()
    if args.command == "update":
        update(args.dry_run)
    elif args.command == "commit-message":
        update_commit_message()
    elif args.command == "prepare":
        prepare(args.staging.resolve())
    else:
        publish(args.staging.resolve())


if __name__ == "__main__":
    main()
