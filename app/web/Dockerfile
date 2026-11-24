# syntax=docker/dockerfile:1.7
# Multi-stage Next.js build (spec 12): dependencies in a cached layer, a build stage, and a
# small runtime with only the standalone server.

FROM node:22-alpine AS deps
WORKDIR /web
COPY package.json package-lock.json ./
# The optional "build_ca" secret is only for building behind a TLS-intercepting proxy.
RUN --mount=type=cache,target=/root/.npm \
    --mount=type=secret,id=build_ca,required=false \
    if [ -f /run/secrets/build_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; \
    npm ci --no-audit --no-fund

FROM node:22-alpine AS build
WORKDIR /web
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /web/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /web
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
COPY --from=build --chown=node:node /web/.next/standalone ./
COPY --from=build --chown=node:node /web/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
