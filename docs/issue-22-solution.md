# Issue #22 — Distributed Edge-Cluster Deployment

## Overview

Adds a Docker Swarm deployment model for Ironclad GM across a Control Plane
(NAS / x86 host) and multiple Edge Worker Nodes (ARM/x86 SBCs), connected via
WireGuard VPN with Wake-on-LAN lifecycle management and multi-arch CI.

## Architecture

```
┌─────────────────────────────────────┐     WireGuard (10.22.0.0/24)
│  Control Plane (NAS / x86)          │───────────────────────────────────┐
│  aetheris-control stack             │                                  │
│  • scribe (orchestrator :8000)     │     ┌──────────────────────┐   │
│  • discord-bot                      │     │ Edge Worker 1 (arm64) │   │
│  • ironclad-db (PostgreSQL 16)      │────▶│ aetheris-worker stack  │   │
│  • ironclad-cache (Redis 7)         │     │ • brain (Ollama)       │   │
│  • ironclad-chroma (ChromaDB)       │     └──────────────────────┘   │
│  • media-proxy (:8001)              │                                  │
│  • pulse (health-sentinel :58291)   │     ┌──────────────────────┐   │
│  • janitor                          │     │ Edge Worker 2 (amd64) │   │
│  • lavalink-audio                   │────▶│ aetheris-worker stack  │   │
└─────────────────────────────────────┘     │ • brain (Ollama)       │   │
                                            └──────────────────────┘   │
                                                                            │
                                            ┌──────────────────────┐   │
                                            │ NFS (shared models)   │───┘
                                            │ /volume1/ollama-models│
                                            └──────────────────────┘
```

## New Files

| File | Purpose |
|------|---------|
| `deploy/swarm/docker-stack.control.yml` | All services except brain; Docker secrets; manager placement |
| `deploy/swarm/docker-stack.worker.yml` | Brain (Ollama) only; Intel GPU passthrough; worker label placement |
| `deploy/swarm/docker-stack.nfs.yml` | NFS volume override for shared Ollama model storage |
| `deploy/wireguard/wg-control.conf.template` | WireGuard control plane config (10.22.0.1/24, port 51820) |
| `deploy/wireguard/wg-worker.conf.template` | WireGuard per-worker config (10.22.0.X/32, keepalive 25s) |
| `deploy/wol/wakeup.sh` | Wake-on-LAN broadcast script (wakeonlan + Python UDP fallback) |
| `deploy/wol/shutdown.sh` | SSH graceful worker shutdown |
| `deploy/wol/wol-nodes.example.json` | Example node configuration |
| `.github/workflows/multiarch-build.yml` | CI: linux/amd64 + linux/arm64 for all 6 services via Buildx/QEMU |
| `orchestrator/services/edge_cluster.py` | EdgeClusterManager: WoL, SSH, node registration, thermal health |
| `orchestrator/tests/test_edge_cluster.py` | Unit tests: 5 classes, 17 test cases |

## Deployment Guide

### 1. Prerequisites

```bash
# On the control plane
docker swarm init

# On each worker (token from swarm init output)
docker swarm join --token <SWARM_JOIN_TOKEN> <CONTROL_PLANE_IP>:2377

# Label worker nodes (run on manager for each worker)
docker node update --label-add aetheris.worker=true <NODE_ID>
```

### 2. Create Docker Secrets

```bash
echo "$POSTGRES_PASSWORD"  | docker secret create postgres_password -
echo "$REDIS_PASSWORD"     | docker secret create redis_password -
echo "$SESSION_SECRET_KEY" | docker secret create session_secret_key -
echo "$DISCORD_BOT_TOKEN" | docker secret create discord_bot_token -
echo "$GEMINI_API_KEY"    | docker secret create gemini_api_key -
echo "$LAVALINK_PASSWORD" | docker secret create lavalink_password -
```

### 3. Deploy Control Stack

```bash
docker stack deploy \
  -c deploy/swarm/docker-stack.control.yml \
  aetheris-control
```

### 4a. Deploy Worker Stack (local model volumes)

```bash
docker stack deploy \
  -c deploy/swarm/docker-stack.worker.yml \
  aetheris-worker
```

### 4b. Deploy Worker Stack (NFS shared models)

```bash
NFS_SERVER_IP=192.168.1.1 NFS_MODELS_PATH=/volume1/ollama-models \
docker stack deploy \
  -c deploy/swarm/docker-stack.worker.yml \
  -c deploy/swarm/docker-stack.nfs.yml \
  aetheris-worker
```

### 5. WireGuard VPN Setup

```bash
# On every node:
wg genkey | tee private.key | wg pubkey > public.key

# Control plane: fill wg-control.conf.template with each worker's public key
sudo cp wg-control.conf /etc/wireguard/wg0.conf
sudo systemctl enable --now wg-quick@wg0

# Each worker: fill wg-worker.conf.template (unique 10.22.0.X address)
sudo cp wg-worker.conf /etc/wireguard/wg0.conf
sudo systemctl enable --now wg-quick@wg0
```

### 6. Wake / Sleep Workers

```bash
# Configure node list
cp deploy/wol/wol-nodes.example.json deploy/wol/wol-nodes.json
# (edit with your node MACs + SSH details)

# Wake all workers
bash deploy/wol/wakeup.sh

# Graceful shutdown
bash deploy/wol/shutdown.sh
```

## NodeRouter Integration

`EdgeClusterManager` writes to the same `node_registry` PostgreSQL table used by `NodeRouter`. After `register_edge_node()` is called the node is immediately eligible for request routing:

- `arch=arm64` or `hardware_tier=standard` → `adjudication` role
- `arch=amd64` + `hardware_tier=high` → `adjudication` + `narrative` roles

## Multi-Arch CI

`.github/workflows/multiarch-build.yml` builds all 6 service images for `linux/amd64` and `linux/arm64` on every push to `main` or `claude/**`. Images are pushed to GHCR with branch + SHA tags; `latest` is tagged only on `main`.

## Secrets Handling Notes

| Service | Secret strategy |
|---------|-----------------|
| PostgreSQL | `POSTGRES_PASSWORD_FILE` native support |
| Redis | Command override: `sh -c 'redis-server --requirepass "$(cat /run/secrets/redis_password)"'` |
| Orchestrator | `*_FILE` env var pattern |
| Lavalink | Environment variable `LAVALINK_SERVER_PASSWORD` set at deploy time (no native `_FILE` support) |
