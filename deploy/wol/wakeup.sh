#!/usr/bin/env bash
# Wake all configured edge workers via Wake-on-LAN magic packet.
# Uses `wakeonlan` binary if available; falls back to Python UDP socket.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODES_FILE="${NODES_FILE:-$SCRIPT_DIR/wol-nodes.json}"

if [[ ! -f "$NODES_FILE" ]]; then
  echo "ERROR: $NODES_FILE not found." >&2
  echo "Copy deploy/wol/wol-nodes.example.json to deploy/wol/wol-nodes.json and fill in your values." >&2
  exit 1
fi

send_wol_python() {
  local mac="$1"
  python3 - <<PYEOF
import socket, sys
mac = "$mac".replace(':', '').replace('-', '')
if len(mac) != 12:
    sys.exit(1)
pkt = bytes.fromhex('ff' * 6 + mac * 16)
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.sendto(pkt, ('255.255.255.255', 9))
print("WoL sent to $mac (Python fallback)")
PYEOF
}

mapfile -t NAMES < <(python3 -c "
import json
d = json.load(open('$NODES_FILE'))
for n in d['workers']: print(n['name'])
")
mapfile -t MACS < <(python3 -c "
import json
d = json.load(open('$NODES_FILE'))
for n in d['workers']: print(n.get('mac_address', ''))
")

for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"
  mac="${MACS[$i]}"

  if [[ -z "$mac" ]]; then
    echo "SKIP $name — no mac_address configured"
    continue
  fi

  echo "Waking $name ($mac)..."
  if command -v wakeonlan &>/dev/null; then
    wakeonlan "$mac"
  else
    send_wol_python "$mac"
  fi
done

echo "All WoL packets sent. Nodes may take 30–90 seconds to boot."
