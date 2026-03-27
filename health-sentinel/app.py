"""
Health Sentinel — Ironclad GM Sidecar (Pulse)
===============================================
Lightweight Flask service on port 58291. Reports orchestrator busy/ok
status and surfaces the last System Integrity Check (SIC) result from
Redis to the Pulse dashboard.

Endpoints
---------
GET /health
    200 {"status": "ok"|"busy", "uptime_s": <float>}

GET /sic
    200 {"status": "healthy"|"unstable"|"critical", "checked_at": ..., "pillars": [...]}
    200 {"status": "unknown"} if no SIC result is cached yet

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

import json
import os
import time

import redis
from flask import Flask, jsonify

app    = Flask(__name__)
_START = time.monotonic()

_BUSY_KEY       = "ironclad:sentinel:busy"
_SIC_RESULT_KEY = "ironclad:sic:result"


def _redis() -> redis.Redis:
    return redis.Redis(
        host              = os.environ.get("REDIS_HOST", "ironclad-cache"),
        port              = int(os.environ.get("REDIS_PORT", 6379)),
        password          = os.environ.get("REDIS_PASSWORD", ""),
        decode_responses  = True,
        socket_connect_timeout = 2,
        socket_timeout    = 2,
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


@app.get("/sic")
def sic():
    """Return the last SIC result written by the orchestrator to Redis."""
    try:
        r   = _redis()
        raw = r.get(_SIC_RESULT_KEY)
        if raw:
            return jsonify(json.loads(raw)), 200
    except redis.RedisError:
        return jsonify({"status": "unknown", "error": "Redis unreachable"}), 200
    except Exception as exc:
        return jsonify({"status": "unknown", "error": str(exc)}), 200

    return jsonify({"status": "unknown", "detail": "No SIC result cached yet."}), 200


@app.get("/ping")
def ping():
    return "pong", 200


if __name__ == "__main__":
    port = int(os.environ.get("SENTINEL_PORT", 58291))
    app.run(host="0.0.0.0", port=port, debug=False)
