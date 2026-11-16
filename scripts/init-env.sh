#!/usr/bin/env bash
# Create .env from .env.example and fill every empty secret with a random value.
# Never overwrites an existing .env (that would orphan the database passwords
# already baked into the volumes). Works with macOS's bash 3.2.
#
# Usage: scripts/init-env.sh [vps <domain>]
#   no argument     local values (*.localhost over http)
#   vps <domain>    https://rallly.<domain>, authentik.<domain>, mail.<domain>
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -e .env ]]; then
	echo ".env already exists; delete it first to regenerate (this resets nothing in the volumes)." >&2
	exit 1
fi

mode="${1:-local}"
domain=""
if [[ "$mode" == vps ]]; then
	domain="${2:?usage: scripts/init-env.sh vps <domain>}"
fi

hex() { openssl rand -hex "$1"; }
# Alphanumeric only: these values end up in URLs (DATABASE_URL) and YAML.
pass() {
	local s
	s="$(openssl rand -base64 96 | tr -dc 'A-Za-z0-9')"
	echo "${s:0:$1}"
}

# Prints the value to write for KEY, or nothing to keep the example's line.
value_for() {
	case "$1" in
	AUTHENTIK_BOOTSTRAP_PASSWORD) pass 24 ;;
	DEMO_PASSWORD) pass 16 ;;
	AUTHENTIK_SECRET_KEY) pass 60 ;;
	AUTHENTIK_DB_PASSWORD | RALLLY_DB_PASSWORD) pass 32 ;;
	RALLLY_SECRET_PASSWORD) pass 48 ;;
	RALLLY_OIDC_CLIENT_SECRET) pass 64 ;;
	GARAGE_RPC_SECRET | S3_SECRET_ACCESS_KEY) hex 32 ;;
	S3_ACCESS_KEY_ID) echo "GK$(hex 12)" ;;
	esac
}

vps_value_for() {
	case "$1" in
	COMPOSE_FILE) echo "compose.yml:compose.vps.yml" ;;
	PUBLIC_SCHEME) echo "https" ;;
	RALLLY_HOST) echo "rallly.$domain" ;;
	AUTHENTIK_HOST) echo "authentik.$domain" ;;
	MAIL_HOST) echo "mail.$domain" ;;
	esac
}

umask 077
while IFS= read -r line; do
	if [[ "$line" =~ ^([A-Z0-9_]+)=(.*)$ ]]; then
		key="${BASH_REMATCH[1]}"
		value="${BASH_REMATCH[2]}"
		[[ -z "$value" ]] && value="$(value_for "$key")"
		if [[ "$mode" == vps ]]; then
			override="$(vps_value_for "$key")"
			[[ -n "$override" ]] && value="$override"
		fi
		line="$key=$value"
	fi
	printf '%s\n' "$line"
done <.env.example >.env

echo "Wrote .env (mode 600)."
echo "  Authentik admin : akadmin / $(grep '^AUTHENTIK_BOOTSTRAP_PASSWORD=' .env | cut -d= -f2-)"
echo "  Demo accounts   : demo-admin, demo-user, demo-denied / $(grep '^DEMO_PASSWORD=' .env | cut -d= -f2-)"
