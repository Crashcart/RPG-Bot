#!/usr/bin/env bash
# Gracefully stop Ollama / brain service on all configured edge workers via SSH.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODES_FILE="${NODES_FILE:-$SCRIPT_DIR/wol-nodes.json}"

if [[ ! -f "$NODES_FILE" ]]; then
  echo "ERROR: $NODES_FILE not found." >&2
  exit 1
fi

mapfile -t NAMES     < <(python3 -c "import json; d=json.load(open('$NODES_FILE')); [print(n['name']) for n in d['workers']]")
mapfile -t SSH_HOSTS < <(python3 -c "import json; d=json.load(open('$NODES_FILE')); [print(n.get('ssh_host','')) for n in d['workers']]")
mapfile -t SSH_USERS < <(python3 -c "import json; d=json.load(open('$NODES_FILE')); [print(n.get('ssh_user','root')) for n in d['workers']]")

for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"
  ssh_host="${SSH_HOSTS[$i]}"
  ssh_user="${SSH_USERS[$i]}"

  if [[ -z "$ssh_host" ]]; then
    echo "SKIP $name — no ssh_host configured"
    continue
  fi

  echo "Shutting down $name ($ssh_user@$ssh_host)..."
  ssh -o StrictHostKeyChecking=no \
      -o ConnectTimeout=15 \
      "${ssh_user}@${ssh_host}" \
      'docker service scale aetheris-worker_brain=0 2>/dev/null || sudo systemctl stop ollama' \
    && echo "OK $name" \
    || echo "WARN: shutdown command failed for $name (may already be offline)"
done

echo "Done."
