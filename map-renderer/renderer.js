/**
 * Aetheris Map Renderer
 * =====================
 * Subscribes to NATS subject `map.update.>` for coordinate/Fog-of-War events,
 * renders the campaign map PNG entirely in RAM using the `canvas` library, and
 * pushes the resulting binary buffer to Redis for the Discord bot to serve.
 *
 * TDR §4 Option 2: Alpine/Node.js + canvas, no disk writes, NATS-driven.
 *
 * NATS subjects consumed
 * ----------------------
 *   map.update.<campaign_id>      – CoordinateUpdatePayload JSON
 *   map.reveal.<campaign_id>      – FogRevealPayload JSON (cells to reveal)
 *   map.reset.<campaign_id>       – resets FoW to fully fogged
 *
 * Redis keys written
 * ------------------
 *   map:png:<campaign_id>         – raw PNG buffer (no TTL — overwritten per update)
 *   map:pos:<campaign_id>:<pid>   – JSON {x, y, token} player position
 *   fow:<campaign_id>             – JSON array of revealed cell indices (flat grid)
 *
 * HTTP endpoints
 * --------------
 *   GET /map/<campaign_id>        – returns the cached PNG for that campaign
 *   GET /health                   – liveness probe
 */

"use strict";

const http    = require("http");
const { createCanvas } = require("canvas");
const Redis   = require("ioredis");
const { connect, StringCodec } = require("nats");

// ── Configuration ─────────────────────────────────────────────────────────────
const NATS_URL    = process.env.NATS_URL    || "nats://aetheris-nats:4222";
const REDIS_HOST  = process.env.REDIS_HOST  || "ironclad-cache";
const REDIS_PORT  = parseInt(process.env.REDIS_PORT || "6379", 10);
const REDIS_PASS  = process.env.REDIS_PASSWORD || "";
const HTTP_PORT   = parseInt(process.env.MAP_RENDERER_PORT || "3001", 10);

// Map canvas dimensions
const TILE_SIZE   = parseInt(process.env.MAP_TILE_SIZE   || "32",  10);
const MAP_COLS    = parseInt(process.env.MAP_COLS        || "20",  10);
const MAP_ROWS    = parseInt(process.env.MAP_ROWS        || "20",  10);
const CANVAS_W    = TILE_SIZE * MAP_COLS;
const CANVAS_H    = TILE_SIZE * MAP_ROWS;

// ── Redis client ──────────────────────────────────────────────────────────────
const redis = new Redis({ host: REDIS_HOST, port: REDIS_PORT, password: REDIS_PASS });

redis.on("error", (err) => console.error("[map-renderer] Redis error:", err.message));

// ── Canvas utilities ──────────────────────────────────────────────────────────

/** Colour tokens per player index (wraps after 8). */
const TOKEN_COLOURS = [
  "#4fc3f7", "#81c784", "#e57373", "#ffb74d",
  "#ba68c8", "#4db6ac", "#f06292", "#fff176",
];

/**
 * Scan Redis keys matching a pattern without blocking.
 * Uses HSCAN-style iteration via the SCAN command.
 *
 * @param {string} pattern
 * @returns {Promise<string[]>}
 */
async function scanKeys(pattern) {
  const keys = [];
  let cursor = "0";
  do {
    const [nextCursor, batch] = await redis.scan(cursor, "MATCH", pattern, "COUNT", 100);
    keys.push(...batch);
    cursor = nextCursor;
  } while (cursor !== "0");
  return keys;
}

/**
 * Rebuild and persist the PNG for a single campaign.
 * Reads player positions and revealed FoW cells from Redis, draws the canvas,
 * and writes the PNG binary back to Redis.
 *
 * @param {string} campaignId
 */
async function renderAndCache(campaignId) {
  const canvas = createCanvas(CANVAS_W, CANVAS_H);
  const ctx    = canvas.getContext("2d");

  // ── Grid background ─────────────────────────────────────────────────────────
  ctx.fillStyle = "#1a1a2e";
  ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);

  ctx.strokeStyle = "#2a2a4e";
  ctx.lineWidth   = 0.5;
  for (let col = 0; col <= MAP_COLS; col++) {
    ctx.beginPath();
    ctx.moveTo(col * TILE_SIZE, 0);
    ctx.lineTo(col * TILE_SIZE, CANVAS_H);
    ctx.stroke();
  }
  for (let row = 0; row <= MAP_ROWS; row++) {
    ctx.beginPath();
    ctx.moveTo(0, row * TILE_SIZE);
    ctx.lineTo(CANVAS_W, row * TILE_SIZE);
    ctx.stroke();
  }

  // ── Revealed cells (Fog of War) ─────────────────────────────────────────────
  const fowRaw = await redis.get(`fow:${campaignId}`);
  const revealed = fowRaw ? new Set(JSON.parse(fowRaw)) : new Set();

  for (const idx of revealed) {
    const col = idx % MAP_COLS;
    const row = Math.floor(idx / MAP_COLS);
    ctx.fillStyle = "#2d3748";
    ctx.fillRect(col * TILE_SIZE + 1, row * TILE_SIZE + 1, TILE_SIZE - 2, TILE_SIZE - 2);
  }

  // ── Fog overlay on unrevealed cells ─────────────────────────────────────────
  ctx.fillStyle = "rgba(0,0,0,0.72)";
  for (let row = 0; row < MAP_ROWS; row++) {
    for (let col = 0; col < MAP_COLS; col++) {
      if (!revealed.has(row * MAP_COLS + col)) {
        ctx.fillRect(col * TILE_SIZE, row * TILE_SIZE, TILE_SIZE, TILE_SIZE);
      }
    }
  }

  // ── Player tokens ───────────────────────────────────────────────────────────
  const posKeys = await scanKeys(`map:pos:${campaignId}:*`);
  let tokenIndex = 0;

  for (const key of posKeys) {
    const raw = await redis.get(key);
    if (!raw) continue;
    try {
      const pos = JSON.parse(raw);
      const cx  = pos.x * TILE_SIZE + TILE_SIZE / 2;
      const cy  = pos.y * TILE_SIZE + TILE_SIZE / 2;
      const r   = TILE_SIZE * 0.38;

      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, 2 * Math.PI);
      ctx.fillStyle = TOKEN_COLOURS[tokenIndex % TOKEN_COLOURS.length];
      ctx.fill();

      if (pos.token) {
        ctx.fillStyle = "#0d0d1a";
        ctx.font      = `bold ${Math.floor(TILE_SIZE * 0.4)}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(pos.token.slice(0, 2).toUpperCase(), cx, cy);
      }

      tokenIndex++;
    } catch (err) {
      console.warn(`[map-renderer] Skipping malformed position key ${key}:`, err.message);
    }
  }

  // ── Write PNG to Redis ───────────────────────────────────────────────────────
  const buf = canvas.toBuffer("image/png");
  await redis.set(`map:png:${campaignId}`, buf);
}

// ── NATS message handlers ─────────────────────────────────────────────────────
//
// FogOfWarService (Python) writes all state to Redis _before_ publishing the
// NATS event. The renderer's job is to read that state and re-render the PNG.
// There is no duplication of position/FoW writes here.

/**
 * Handle `map.update.<campaign_id>`.
 * FogOfWarService has already updated map:pos:* and fow:* in Redis.
 * We simply re-render the canvas from the current Redis state.
 */
async function handleUpdate(campaignId, _payload) {
  await renderAndCache(campaignId);
  console.log(`[map-renderer] Re-rendered map for campaign ${campaignId} (position update)`);
}

/**
 * Handle `map.reveal.<campaign_id>`.
 * FogOfWarService has already updated fow:* in Redis.
 */
async function handleReveal(campaignId, _payload) {
  await renderAndCache(campaignId);
  console.log(`[map-renderer] Re-rendered map for campaign ${campaignId} (fog reveal)`);
}

/**
 * Handle `map.reset.<campaign_id>`.
 * FogOfWarService has already deleted fow:* and map:pos:* from Redis.
 */
async function handleReset(campaignId) {
  // Clear any cached PNG so the next GET triggers a fresh (empty) render
  await redis.del(`map:png:${campaignId}`);
  await renderAndCache(campaignId);
  console.log(`[map-renderer] Reset map for campaign ${campaignId}`);
}

// ── HTTP server — serves cached PNG maps ──────────────────────────────────────
const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok" }));
    return;
  }

  const mapMatch = req.url && req.url.match(/^\/map\/([^/]+)$/);
  if (req.method === "GET" && mapMatch) {
    const campaignId = mapMatch[1];
    try {
      const buf = await redis.getBuffer(`map:png:${campaignId}`);
      if (!buf) {
        // Render on demand if no cached image exists yet
        await renderAndCache(campaignId);
        const freshBuf = await redis.getBuffer(`map:png:${campaignId}`);
        if (!freshBuf) {
          res.writeHead(404, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "map not found" }));
          return;
        }
        res.writeHead(200, { "Content-Type": "image/png", "Cache-Control": "no-cache" });
        res.end(freshBuf);
        return;
      }
      res.writeHead(200, { "Content-Type": "image/png", "Cache-Control": "no-cache" });
      res.end(buf);
    } catch (err) {
      console.error("[map-renderer] HTTP error:", err.message);
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "internal error" }));
    }
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "not found" }));
});

// ── Bootstrap ─────────────────────────────────────────────────────────────────
(async () => {
  console.log(`[map-renderer] Connecting to NATS at ${NATS_URL}…`);
  const nc = await connect({ servers: NATS_URL });
  const sc = StringCodec();
  console.log("[map-renderer] NATS connected.");

  // Subscribe to all map subjects using wildcard
  const sub = nc.subscribe("map.>");

  (async () => {
    for await (const msg of sub) {
      const subject = msg.subject;
      const parts   = subject.split(".");
      if (parts.length < 3) continue;

      const verb       = parts[1];           // update | reveal | reset
      const campaignId = parts.slice(2).join(".");

      let payload = {};
      try {
        payload = JSON.parse(sc.decode(msg.data));
      } catch (err) {
        console.debug(`[map-renderer] Could not parse payload for ${subject}:`, err.message);
      }

      try {
        if (verb === "update") await handleUpdate(campaignId, payload);
        else if (verb === "reveal") await handleReveal(campaignId, payload);
        else if (verb === "reset")  await handleReset(campaignId);
      } catch (err) {
        console.error(`[map-renderer] Error handling ${subject}:`, err.message);
      }
    }
  })();

  server.listen(HTTP_PORT, () => {
    console.log(`[map-renderer] HTTP server listening on :${HTTP_PORT}`);
  });

  // Graceful shutdown
  process.on("SIGTERM", async () => {
    console.log("[map-renderer] Shutting down…");
    await nc.drain();
    server.close(() => process.exit(0));
  });
})();
