#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGE_PUB_KEY="${SCRIPT_DIR}/keys/age-public-key.txt"

generate_token() { openssl rand -hex 24; }

if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
    cat > "${SCRIPT_DIR}/.env" <<EOF
# StreamGremlin Environment Secrets
PORT=4736
SESSION_SECRET=$(generate_token)
AGENT_TOKEN=$(generate_token)
WEBHOOK_TOKEN=$(generate_token)
ADMIN_PASSWORD=admin
STREAMER_PASSWORD=streamer
MOD_PASSWORD=mod
TWITCH_CLIENT_ID=
TWITCH_CLIENT_SECRET=
TWITCH_BROADCASTER_CHANNEL=
TWITCH_BOT_CHANNEL=
HA_BASE_URL=https://home.orfel.de
HA_ACCESS_TOKEN=
N8N_WEBHOOK_URL=
EOF
    chmod 600 "${SCRIPT_DIR}/.env"
    echo "Generated new .env file."
fi

if [[ -f "${AGE_PUB_KEY}" ]] && command -v age >/dev/null 2>&1; then
    age -r "$(tr -d '\r\n' < "${AGE_PUB_KEY}")" -o "${SCRIPT_DIR}/.env.age" "${SCRIPT_DIR}/.env"
    echo "Encrypted .env -> .env.age"
else
    echo "Warning: age binary or public key not found, skipped .env.age encryption."
fi
