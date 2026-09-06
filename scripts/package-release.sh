#!/usr/bin/env bash
# Packages a tagged release as a zip containing only the files a manually
# installed plugin actually needs — not the whole repo (tests, docs, dev
# tooling) — under a fixed, dot-free top-level directory name
# ("picard-file-health"), not GitHub's auto-generated "<repo>-<tag>"
# naming (e.g. "picard-file-health-1.1.2").
#
# A folder name containing dots breaks the plugin when it's manually
# unzipped straight into Picard's plugins directory: Picard's local-plugin
# discovery derives the module's dotted import path from the directory
# name, and `__init__.py`'s `from . import analysis` relative import can't
# resolve through the spurious extra "packages" each dot introduces (see
# tests/conftest.py's `plugin` fixture docstring for the same constraint
# affecting hyphens). This script keeps the *zip filename* versioned so
# releases stay distinguishable in a downloads folder, while pinning the
# *extracted directory name* to something always import-safe.
#
# Usage: scripts/package-release.sh <tag>
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <tag>" >&2
    exit 1
fi

tag="$1"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_dir="$repo_root/dist"
out_file="$out_dir/picard-file-health-${tag#v}.zip"

# Only what Picard actually loads/needs at runtime, plus the license and
# user-facing docs a manual install would want on disk. Excludes tests/,
# docs/ (design-intent tree), scripts/, and every dev/agent config file.
payload=(
    MANIFEST.toml
    __init__.py
    analysis.py
    help_content.py
    LICENSE
    README.md
    QUICKSTART.md
)

mkdir -p "$out_dir"
git -C "$repo_root" archive --format=zip --prefix=picard-file-health/ "$tag" -o "$out_file" -- "${payload[@]}"

echo "Wrote $out_file"
