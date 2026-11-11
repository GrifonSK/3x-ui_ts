#!/bin/sh
set -e

CONFIG_PATH="${CONFIG_PATH:-/config/config.yaml}"
export CONFIG_PATH

echo "[entrypoint] using config: $CONFIG_PATH"

# 1. panel + subscription settings in the 3x-ui DB (before the panel starts)
python3 /configure.py --pre

# 2. Tailscale (userspace by default, exit node on the VPS)
python3 /configure.py --tailscale || echo "[entrypoint] tailscale setup failed, continuing"

# 3. official 3x-ui entrypoint (fail2ban/acme + xray + panel)
/app/DockerEntrypoint.sh &
PANEL_PID=$!

# 4. declarative inbounds / xray routing via the panel API
python3 /configure.py --post || echo "[entrypoint] post-configuration failed (panel is still up)"

wait "$PANEL_PID"
