FROM node:22-alpine AS build

WORKDIR /src/frontend

COPY frontend/package*.json ./
RUN npm install --no-audit --no-fund

COPY frontend/ ./

ARG VITE_RESOURCEPLANNER_ENVIRONMENT=PROD
ENV VITE_RESOURCEPLANNER_ENVIRONMENT=${VITE_RESOURCEPLANNER_ENVIRONMENT}

RUN npm run build

FROM nginx:1.28-alpine AS runtime

ENV NGINX_ENVSUBST_FILTER=RESOURCEPLANNER_FRAME_ANCESTORS \
    RESOURCEPLANNER_FRAME_ANCESTORS="'self'"

COPY frontend/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /src/frontend/dist /usr/share/nginx/html

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1
