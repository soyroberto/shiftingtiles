#!/usr/bin/env python3
"""Package the existing public gallery without copying repository internals.

This helper makes no Azure calls and never deletes or modifies source files.
Generated OneDrive assets and manifests are intentionally not packaged.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REQUIRED_FILES = frozenset(
    {
        "index.html",
        "jquery-1.8.3.js",
        "less-2.5.1.min.js",
        "shiftingtiles.js",
        "shiftingtiles.less",
    }
)
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"})


def tracked_files(repo: Path) -> set[str]:
    raw = subprocess.check_output(["git", "-C", str(repo), "ls-files", "-z"])
    return {name.decode("utf-8") for name in raw.split(b"\0") if name}


def select_files(tracked: set[str]) -> list[str]:
    """Allow only required entry files and tracked images at repository root."""
    missing = REQUIRED_FILES - tracked
    if missing:
        raise ValueError("Required files are not tracked: " + ", ".join(sorted(missing)))
    return sorted(
        name
        for name in tracked
        if "/" not in name
        and not name.startswith(".")
        and (name in REQUIRED_FILES or Path(name).suffix.lower() in IMAGE_EXTENSIONS)
    )


def package(repo: Path, output: Path) -> list[str]:
    repo = repo.resolve(strict=True)
    git_root = Path(
        subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"], text=True
        ).strip()
    ).resolve()
    if repo != git_root:
        raise ValueError("--repo must be the repository root")
    if output.is_symlink():
        raise ValueError("The package output must not be a symbolic link")
    output = output.resolve()
    # A bad output path must never overwrite application files or Git internals.
    if output == repo or output in repo.parents:
        raise ValueError("The package output cannot be the repository or an ancestor")
    if output.is_relative_to(repo):
        relative = output.relative_to(repo)
        if relative.parts[0] != ".work":
            raise ValueError("In-repository package output must be beneath .work/")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("The package output must be a new or empty directory")

    selected = select_files(tracked_files(repo))
    for name in selected:
        source = repo / name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Public asset must be a regular, non-symlink file: {name}")

    images = [name for name in selected if Path(name).suffix.lower() in IMAGE_EXTENSIONS]
    if not images:
        raise ValueError("No tracked root-level images are available for this gallery")

    # Catch legacy filename mistakes, without rewriting the website in deployment.
    html = (repo / "index.html").read_text(encoding="utf-8")
    image_refs = set(re.findall(r'[\"\']([^\"\']+\.(?:jpe?g|png|webp|gif|avif))[\"\']', html, re.I))
    missing_refs = sorted(image_refs - set(selected))
    if missing_refs:
        print(
            "WARNING: Existing gallery references images not in the package: "
            + ", ".join(missing_refs),
            file=sys.stderr,
        )

    assets = output / "assets"
    entrypoint = output / "entrypoint"
    assets.mkdir(parents=True)
    entrypoint.mkdir()
    for name in selected:
        destination = entrypoint if name == "index.html" else assets
        shutil.copyfile(repo / name, destination / name)
    print(f"Packaged {len(selected)} public files, including {len(images)} images.")
    print(f"Assets: {assets}")
    print(f"Entrypoint: {entrypoint / 'index.html'}")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        package(args.repo, args.output)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
