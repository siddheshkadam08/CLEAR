# syntax=docker/dockerfile:1.7
#
# Frontend image.
#
# Multi-stage with a named `runtime` target, because compose targets it explicitly.
# The build args are baked in at build time: Vite substitutes `import.meta.env`
# during the bundle, so they cannot be supplied as runtime env vars on the
# container - a value passed only at `docker run` would silently be the default.

# =============================================================================
# deps - cached separately from the source so a code edit does not reinstall
# =============================================================================
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
# `npm ci` when there is a lockfile, `npm install` when there is not, so a fresh
# checkout without a committed lockfile still builds.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# =============================================================================
# build
# =============================================================================
FROM node:22-alpine AS build
WORKDIR /app

ARG VITE_API_BASE_URL=/api/v1
ARG VITE_ENABLE_MICROSOFT_SSO=false
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL \
    VITE_ENABLE_MICROSOFT_SSO=$VITE_ENABLE_MICROSOFT_SSO

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Typecheck in the image as well as in CI: a type error that only fails in CI is
# a type error that ships to whoever built the image by hand.
RUN npm run typecheck && npm run build

# =============================================================================
# dev - Vite dev server with hot reload, used by docker-compose.override.yml
#
# Serves from source rather than a built bundle, so `VITE_API_BASE_URL` is read at
# runtime instead of being baked in at build time like the runtime stage.
# =============================================================================
FROM node:22-alpine AS dev
WORKDIR /app
ENV NODE_ENV=development

COPY --from=deps /app/node_modules ./node_modules
COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# =============================================================================
# runtime
# =============================================================================
FROM nginx:1.27-alpine AS runtime

ARG API_HOST=api
ARG API_PORT=8000
ENV API_HOST=$API_HOST \
    API_PORT=$API_PORT

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/templates/default.conf.template

# nginx:alpine substitutes ${API_HOST}/${API_PORT} from the template on start, so
# the upstream is configurable without rebuilding the image.
EXPOSE 8080

# Runs unprivileged: nothing in a static file server needs root, and port 8080
# avoids needing the capability to bind a privileged port.
USER nginx

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --quiet --spider http://localhost:8080/health || exit 1
