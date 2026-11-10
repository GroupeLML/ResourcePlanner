ARG RESOURCEPLANNER_GIT_REVISION=unavailable

FROM node:22-alpine AS build

ARG RESOURCEPLANNER_GIT_REVISION

WORKDIR /src/frontend

COPY frontend/package*.json ./
RUN npm install --no-audit --no-fund

COPY frontend/ ./

RUN printf '%s' "$RESOURCEPLANNER_GIT_REVISION" | grep -Eq '^(unavailable|[0-9a-fA-F]{40})$' \
    && printf 'window.RESOURCEPLANNER_BUILD = Object.freeze({ revision: "%s" });\n' "$RESOURCEPLANNER_GIT_REVISION" > public/build-meta.js \
    && npm run build

FROM nginx:1.28-alpine AS runtime

ARG RESOURCEPLANNER_GIT_REVISION
LABEL org.opencontainers.image.revision="$RESOURCEPLANNER_GIT_REVISION"

ENV NGINX_ENVSUBST_FILTER=RESOURCEPLANNER_FRAME_ANCESTORS \
    RESOURCEPLANNER_FRAME_ANCESTORS="'self'" \
    RESOURCEPLANNER_ENVIRONMENT=PROD

COPY frontend/default.conf.template /etc/nginx/templates/default.conf.template
COPY frontend/20-resourceplanner-runtime-config.sh /docker-entrypoint.d/20-resourceplanner-runtime-config.sh
RUN chmod +x /docker-entrypoint.d/20-resourceplanner-runtime-config.sh
COPY --from=build /src/frontend/dist /usr/share/nginx/html

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1
