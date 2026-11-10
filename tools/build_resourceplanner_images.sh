#!/bin/sh
set -eu

repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$repo_root"

revision="$(git rev-parse --verify HEAD)"
if ! printf '%s' "$revision" | grep -Eq '^[0-9a-fA-F]{40}$'; then
  echo "Unable to derive a full Git revision for the ResourcePlanner image build." >&2
  exit 1
fi

if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
  echo "Refusing to build revision metadata from a dirty checkout." >&2
  echo "Commit or remove source changes before building deployable images." >&2
  exit 1
fi

RESOURCEPLANNER_GIT_REVISION="$(printf '%s' "$revision" | tr '[:upper:]' '[:lower:]')"
export RESOURCEPLANNER_GIT_REVISION

echo "Building ResourcePlanner images from Git revision: $RESOURCEPLANNER_GIT_REVISION"
exec docker compose build "$@"
