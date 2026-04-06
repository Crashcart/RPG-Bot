# ─────────────────────────────────────────────────────────────────────────────
# Map Renderer — Alpine/Node.js Fog-of-War PNG renderer
# TDR §4 Option 2: lightweight canvas-based map rendering.
# Subscribes to NATS for coordinate updates, draws the PNG entirely in RAM,
# and caches the image buffer in Redis for the Discord bot to serve.
# ─────────────────────────────────────────────────────────────────────────────
FROM node:22-alpine

WORKDIR /app

# canvas requires native build deps on Alpine
RUN apk add --no-cache \
    python3 \
    make \
    g++ \
    cairo-dev \
    pango-dev \
    jpeg-dev \
    giflib-dev \
    librsvg-dev

COPY package.json package-lock.json* ./
RUN npm ci --omit=dev

COPY renderer.js ./

EXPOSE 3001

CMD ["node", "renderer.js"]
