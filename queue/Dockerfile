# ---- build ------------------------------------------------------------------
FROM node:20-alpine AS build
WORKDIR /app

# Dependencies first: this layer is cached unless the manifests change.
COPY package.json package-lock.json* ./
RUN npm ci || npm install

COPY tsconfig.json ./
COPY src ./src
RUN npm run build

# ---- dev --------------------------------------------------------------------
# Hot-reload target used by docker-compose.override.yml. Keeps devDependencies
# (tsx/tsc) that the runtime stage deliberately drops, and runs as root so a
# bind-mounted ./queue/src stays writable from the host under rootless Podman.
FROM node:20-alpine AS dev
WORKDIR /app
ENV NODE_ENV=development

COPY package.json package-lock.json* ./
RUN npm ci || npm install

COPY tsconfig.json ./
COPY src ./src

EXPOSE 9100
CMD ["npm", "run", "dev"]

# ---- runtime ----------------------------------------------------------------
FROM node:20-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production

COPY package.json package-lock.json* ./
RUN (npm ci --omit=dev || npm install --omit=dev) && npm cache clean --force

COPY --from=build /app/dist ./dist

# Non-root: the dispatcher needs no filesystem access beyond its own code.
USER node

EXPOSE 9100
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:9100/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "dist/index.js"]
