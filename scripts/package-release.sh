#!/usr/bin/env bash
# Packages a tagged release as a zip whose top-level directory is a fixed,
# dot-free name ("picard-file-health"), not GitHub's auto-generated
# "<repo>-<tag>" naming (e.g. "picard-file-health-1.1.2").
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

mkdir -p "$out_dir"
git -C "$repo_root" archive --format=zip --prefix=picard-file-health/ "$tag" -o "$out_file"

echo "Wrote $out_file"
