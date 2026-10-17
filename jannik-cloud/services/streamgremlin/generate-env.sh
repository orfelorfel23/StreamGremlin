#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
AGE_PUB_KEY="${REPO_ROOT}/keys/age-public-key.txt"

generate_secret() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32; }

SESSION_SECRET=$(generate_secret)
AGENT_TOKEN=$(generate_secret)
WEBHOOK_SECRET=$(generate_secret)
ADMIN_INIT_PASSWORD=$(generate_secret)

cat > "${SCRIPT_DIR}/.env" <<EOF
# StreamGremlin (Jannik-Cloud)
PORT=4736
PUBLIC_URL=https://streamgremlin.orfel.de
SESSION_SECRET=${SESSION_SECRET}
AGENT_TOKEN=${AGENT_TOKEN}
WEBHOOK_SECRET=${WEBHOOK_SECRET}
ADMIN_PASSWORD=${ADMIN_INIT_PASSWORD}
TWITCH_CLIENT_ID=
TWITCH_CLIENT_SECRET=
TWITCH_BROADCASTER_NAME=
HA_URL=
HA_TOKEN=
EOF

chmod 600 "${SCRIPT_DIR}/.env"

if [[ -f "${AGE_PUB_KEY}" ]]; then
    age -r "$(cat "${AGE_PUB_KEY}")" -o "${SCRIPT_DIR}/.env.age" "${SCRIPT_DIR}/.env"
    echo "Encrypted .env -> .env.age"
fi
echo "StreamGremlin environment generated."
