#!/bin/sh
set -eu

usage() {
  echo "usage: $0 --prev-sha <40hex> --feature-sha <40hex> --output-dir <dir>" >&2
  exit 2
}

prev_sha=""
feature_sha=""
output_dir=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prev-sha) prev_sha="${2:-}"; shift 2 ;;
    --feature-sha) feature_sha="${2:-}"; shift 2 ;;
    --output-dir) output_dir="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done

[ -n "$prev_sha" ] && [ -n "$feature_sha" ] && [ -n "$output_dir" ] || usage
printf '%s' "$prev_sha" | grep -Eq '^[0-9a-fA-F]{40}$' || usage
printf '%s' "$feature_sha" | grep -Eq '^[0-9a-fA-F]{40}$' || usage

git cat-file -e "${prev_sha}^{commit}"
git cat-file -e "${feature_sha}^{commit}"
git cat-file -e "${feature_sha}:migrations/versions/0038_emergency_broadcasts.py"

tmp="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp"
}
trap cleanup EXIT INT TERM

context="$tmp/context"
mkdir -p "$context" "$output_dir"
git archive "$prev_sha" | tar -x -C "$context"
mkdir -p "$context/migrations/versions"
git show "${feature_sha}:migrations/versions/0038_emergency_broadcasts.py"   > "$context/migrations/versions/0038_emergency_broadcasts.py"

prev_short="$(printf '%s' "$prev_sha" | cut -c1-12 | tr '[:upper:]' '[:lower:]')"
feature_short="$(printf '%s' "$feature_sha" | cut -c1-12 | tr '[:upper:]' '[:lower:]')"
tag="rollback-894-${prev_short}-schema0038-${feature_short}"
backend_image="resourceplanner-backend:${tag}"
frontend_image="resourceplanner-frontend:${tag}"

docker build   -f "$context/Dockerfile.backend"   --build-arg "RESOURCEPLANNER_GIT_REVISION=$prev_sha"   -t "$backend_image"   "$context"
docker build   -f "$context/frontend/Dockerfile"   --build-arg "RESOURCEPLANNER_GIT_REVISION=$prev_sha"   -t "$frontend_image"   "$context"

backend_revision="$(docker image inspect "$backend_image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
frontend_revision="$(docker image inspect "$frontend_image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
[ "$(printf '%s' "$backend_revision" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$prev_sha" | tr '[:upper:]' '[:lower:]')" ]
[ "$(printf '%s' "$frontend_revision" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$prev_sha" | tr '[:upper:]' '[:lower:]')" ]

migration_sha256="$(sha256sum "$context/migrations/versions/0038_emergency_broadcasts.py" | awk '{print $1}')"
backend_id="$(docker image inspect "$backend_image" --format '{{.Id}}')"
frontend_id="$(docker image inspect "$frontend_image" --format '{{.Id}}')"

manifest="$output_dir/issue-894-rollback-bundle.json"
python - "$manifest" "$tag" "$prev_sha" "$feature_sha" "$migration_sha256"   "$backend_image" "$backend_id" "$backend_revision"   "$frontend_image" "$frontend_id" "$frontend_revision" <<'PY'
import json
import sys

(
    manifest,
    tag,
    prev_sha,
    feature_sha,
    migration_sha256,
    backend_image,
    backend_id,
    backend_revision,
    frontend_image,
    frontend_id,
    frontend_revision,
) = sys.argv[1:]

payload = {
    "schema": "issue-894-rollback-bundle-v1",
    "issue": 894,
    "tag": tag,
    "application_behavior_revision": prev_sha.lower(),
    "schema_lineage_source_revision": feature_sha.lower(),
    "schema_head": "0038_emergency_broadcasts",
    "migration_overlay": {
        "path": "migrations/versions/0038_emergency_broadcasts.py",
        "sha256": migration_sha256,
    },
    "images": {
        "backend": {
            "tag": backend_image,
            "image_id": backend_id,
            "oci_revision": backend_revision.lower(),
        },
        "frontend": {
            "tag": frontend_image,
            "image_id": frontend_id,
            "oci_revision": frontend_revision.lower(),
        },
    },
    "rollback_rule": "forward_schema_only_no_alembic_downgrade",
}
with open(manifest, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

archive="$output_dir/issue-894-rollback-bundle.tar.gz"
docker save "$backend_image" "$frontend_image" | gzip -9 > "$archive"
printf '%s\n' "$tag" > "$output_dir/issue-894-rollback-tag.txt"

echo "Rollback bundle tag: $tag"
echo "Manifest: $manifest"
echo "Archive: $archive"
