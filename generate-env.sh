#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGE_PUB_KEY="${SCRIPT_DIR}/keys/age-public-key.txt"

generate_token() { openssl rand -hex 24; }
generate_agent_token() { echo "sg_agent_$(openssl rand -hex 16)"; }
generate_webhook_token() { echo "sg_hook_$(openssl rand -hex 16)"; }

create_env_if_missing() {
    local env_name="$1"
    local desc="$2"
    local admin_pw="$3"
    local streamer_pw="$4"
    local mod_pw="$5"

    local file_path="${SCRIPT_DIR}/${env_name}"
    local age_path="${SCRIPT_DIR}/${env_name}.age"

    if [[ ! -f "${file_path}" ]]; then
        cat > "${file_path}" <<EOF
# StreamGremlin Secrets - ${desc}
PORT=4736
SESSION_SECRET=$(generate_token)
AGENT_TOKEN=$(generate_agent_token)
WEBHOOK_TOKEN=$(generate_webhook_token)
ADMIN_PASSWORD=${admin_pw}
STREAMER_PASSWORD=${streamer_pw}
MOD_PASSWORD=${mod_pw}
TWITCH_CLIENT_ID=
TWITCH_CLIENT_SECRET=
TWITCH_BROADCASTER_CHANNEL=
TWITCH_BOT_CHANNEL=
HA_BASE_URL=https://home.orfel.de
HA_ACCESS_TOKEN=
N8N_WEBHOOK_URL=
EOF
        chmod 600 "${file_path}"
        echo "Generated new ${env_name} (${desc})"
    fi

    if [[ -f "${AGE_PUB_KEY}" ]] && command -v age >/dev/null 2>&1; then
        age -r "$(tr -d '\r\n' < "${AGE_PUB_KEY}")" -o "${age_path}" "${file_path}"
        echo "Encrypted ${env_name} -> ${env_name}.age"
    fi
}

# General Development Environment
create_env_if_missing ".env" "General Development Environment" "admin" "streamer" "mod"

echo "StreamGremlin general environment ready."
