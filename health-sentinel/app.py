"""
Health Sentinel — Ironclad GM Sidecar
======================================
Lightweight Flask service on port 58291 that exposes a single health
endpoint reflecting whether the orchestrator is currently processing
heavy AI or image tasks.

The orchestrator sets the Redis key `ironclad:sentinel:busy` (with a
short TTL) when it begins a heavy task and clears it on completion.
This sidecar reads that key and returns the appropriate status.

Endpoints
---------
GET /health
    200 {"status": "ok",   "uptime_s": <float>}
    200 {"status": "busy", "uptime_s": <float>, "reason": <str>}

GET /ping
    200 "pong"

Environment variables
---------------------
REDIS_HOST      Host of the Redis instance (default: ironclad-cache)
REDIS_PORT      Port (default: 6379)
REDIS_PASSWORD  Redis password (required)
SENTINEL_PORT   Port to bind (default: 58291)
"""

from __future__ import annotations

import os
import time

import redis
from flask import Flask, jsonify

app   = Flask(__name__)
_START = time.monotonic()

_BUSY_KEY = "ironclad:sentinel:busy"

def _redis() -> redis.Redis:
    return redis.Redis(
        host     = os.environ.get("REDIS_HOST", "ironclad-cache"),
        port     = int(os.environ.get("REDIS_PORT", 6379)),
        password = os.environ.get("REDIS_PASSWORD", ""),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


@app.get("/health")
def health():
    uptime = round(time.monotonic() - _START, 1)
    try:
        r      = _redis()
        reason = r.get(_BUSY_KEY)
        if reason:
            return jsonify({"status": "busy", "uptime_s": uptime, "reason": reason}), 200
    except redis.RedisError:
        # Redis unreachable — report ok so orchestrator isn't blocked
        return jsonify({"status": "ok", "uptime_s": uptime, "redis": "unreachable"}), 200

    return jsonify({"status": "ok", "uptime_s": uptime}), 200


@app.get("/ping")
def ping():
    return "pong", 200


if __name__ == "__main__":
    port = int(os.environ.get("SENTINEL_PORT", 58291))
    app.run(host="0.0.0.0", port=port, debug=False)
