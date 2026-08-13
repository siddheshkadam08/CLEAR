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

# The API upstream, substituted into nginx.conf at container start. The only
# runtime wiring this image needs: document bytes are served by the API from local
# filesystem storage, so there is no object-storage upstream to configure.
ARG API_HOST=api
ARG API_PORT=8000
ENV API_HOST=$API_HOST \
    API_PORT=$API_PORT

# Opts into the stock `15-local-resolvers.envsh`, which exports the
# NGINX_LOCAL_RESOLVERS that the `resolver` directive in nginx.conf depends on.
#
# The script is **opt-in**, which is easy to miss - its second line is:
#
#     [ "${NGINX_ENTRYPOINT_LOCAL_RESOLVERS:-}" ] || return 0
#
# Without this the script returns immediately, the variable is never exported,
# envsubst leaves the literal `${NGINX_LOCAL_RESOLVERS}` in the generated config,
# and nginx exits 1 at startup with `host not found in resolver
# "${NGINX_LOCAL_RESOLVERS}"`. The container then restart-loops and nothing is
# served on 8080 at all.
#
# It belongs in the image rather than in compose: the config file cannot work
# without it, so a deployment must not be able to omit it.
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/templates/default.conf.template

# Make the paths nginx writes to at startup owned by the user it will run as.
#
# The stock image expects to start as root and drop privileges per worker, so
# these are root-owned. Running the master as `nginx` without this fails twice
# over, and neither message names the cause clearly:
#
#   20-envsubst-on-templates.sh: ERROR: /etc/nginx/templates exists,
#                                but /etc/nginx/conf.d is not writable
#   [emerg] mkdir() "/var/cache/nginx/client_temp" failed (13: Permission denied)
#
# The first silently leaves the container with no server block at all; the second
# stops nginx from starting. The temp directories are created explicitly rather
# than left to nginx, because it creates them lazily on first use - so a container
# can come up healthy and then fail on the first request large enough to buffer.
RUN mkdir -p /var/cache/nginx/client_temp \
             /var/cache/nginx/proxy_temp \
             /var/cache/nginx/fastcgi_temp \
             /var/cache/nginx/uwsgi_temp \
             /var/cache/nginx/scgi_temp \
 && touch /var/run/nginx.pid \
 && chown -R nginx:nginx /var/cache/nginx /var/run/nginx.pid /etc/nginx/conf.d

# nginx:alpine substitutes ${API_HOST}/${API_PORT} from the template on start, so
# the upstream is configurable without rebuilding the image.
EXPOSE 8080

# Runs unprivileged: nothing in a static file server needs root, and port 8080
# avoids needing the capability to bind a privileged port.
USER nginx

# `127.0.0.1`, not `localhost`. Inside the container localhost resolves to ::1
# first, and the server block only carries `listen 8080` - IPv4. The stock
# 10-listen-on-ipv6-by-default.sh would have added `listen [::]:8080`, but it
# patches /etc/nginx/conf.d/default.conf *before* 20-envsubst-on-templates.sh
# regenerates that same file from our template, so its edit is overwritten and
# the final config is IPv4-only.
#
# The result is a container that serves every real request correctly - clients
# reach the published port over IPv4 - while reporting `unhealthy` forever,
# because the probe is the one caller using the IPv6 loopback. Addressing IPv4
# explicitly is safer than adding a `listen [::]:8080` the host may not support.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --quiet --spider http://127.0.0.1:8080/health || exit 1
