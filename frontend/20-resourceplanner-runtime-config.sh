#!/bin/sh
set -eu

environment="$(printf '%s' "${RESOURCEPLANNER_ENVIRONMENT:-PROD}" | tr '[:lower:]' '[:upper:]')"

case "$environment" in
  DEV|PROD)
    ;;
  *)
    echo "RESOURCEPLANNER_ENVIRONMENT must be DEV or PROD (received: $environment)" >&2
    exit 1
    ;;
esac

cat > /usr/share/nginx/html/runtime-config.js <<EOF
window.RESOURCEPLANNER_CONFIG = Object.freeze({ environment: "$environment" });
EOF
