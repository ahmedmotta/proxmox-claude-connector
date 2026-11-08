#!/usr/bin/env bash
# Proxmox Claude Connector - installer
#
# Run as root inside a small Debian 11/12/13 container or VM (recommended), or on a Proxmox node.
#   curl -fsSL https://raw.githubusercontent.com/ahmedmotta/proxmox-claude-connector/main/deploy/install.sh | bash
#
# Options (environment variables):
#   CF_TUNNEL_TOKEN=...   also install cloudflared and connect this Cloudflare Tunnel
#   CF_TUNNEL_TOKEN_FILE  same, read from a file (keeps the token out of the process list)
#   PVE_CONNECTOR_SOURCE  pip install source (default: git+https://github.com/ahmedmotta/proxmox-claude-connector.git)
#   NONINTERACTIVE=1      never prompt; /etc/pve-connector/env must already exist
set -euo pipefail

REPO="ahmedmotta/proxmox-claude-connector"
SOURCE="${PVE_CONNECTOR_SOURCE:-git+https://github.com/${REPO}.git}"
PREFIX=/opt/pve-connector
CONF_DIR=/etc/pve-connector
ENV_FILE=$CONF_DIR/env
DATA_DIR=/var/lib/pve-connector
SVC_USER=pve-connector

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }
ask()  { # ask VAR "question" "default"
  local __var=$1 __q=$2 __def=${3:-} __ans
  if [[ -n "${NONINTERACTIVE:-}" ]]; then printf -v "$__var" '%s' "$__def"; return; fi
  read -r -p "$__q${__def:+ [$__def]}: " __ans </dev/tty || true
  printf -v "$__var" '%s' "${__ans:-$__def}"
}
ask_secret() {
  local __var=$1 __q=$2 __ans
  read -r -s -p "$__q: " __ans </dev/tty || true; echo
  printf -v "$__var" '%s' "$__ans"
}

[[ $EUID -eq 0 ]] || die "run as root"
command -v apt-get >/dev/null || die "this installer supports Debian/Ubuntu (apt)"

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates openssh-client >/dev/null

say "Creating service user and folders"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$SVC_USER"
install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR"
install -d -m 0750 -o root -g "$SVC_USER" "$CONF_DIR"

say "Installing the connector into $PREFIX/venv"
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  python3 -m venv "$PREFIX/venv"
else
  # Debian 11 and other older systems ship Python < 3.11: fetch a standalone Python 3.12 with uv.
  say "System Python is older than 3.11; installing Python 3.12 for the connector"
  if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null \
      || python3 -m pip install -q uv
  fi
  export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
  uv python install 3.12 >/dev/null
  rm -rf "$PREFIX/venv"
  uv venv --quiet --seed --python 3.12 "$PREFIX/venv"
  chmod -R a+rX "$PREFIX/python"
fi
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q --upgrade "$SOURCE"
ln -sf "$PREFIX/venv/bin/pve-connector" /usr/local/bin/pve-connector

if [[ ! -f "$ENV_FILE" ]]; then
  [[ -n "${NONINTERACTIVE:-}" ]] && die "$ENV_FILE is missing and NONINTERACTIVE is set"
  say "Configuring (answers go to $ENV_FILE)"
  ask PVE_HOST        "Proxmox host IP or name" ""
  ask PVE_TOKEN_ID    "API token id (user@realm!name)" "root@pam!claude"
  ask_secret PVE_TOKEN_SECRET "API token secret"
  ask PVE_VERIFY_SSL  "Verify the Proxmox TLS certificate? (true/false)" "false"
  ask PUBLIC_URL      "Public HTTPS URL for Claude (e.g. https://pve-mcp.example.com)" ""
  while :; do
    ask MODE          "Permission mode (full/safe/readonly)" "full"
    [[ $MODE =~ ^(full|safe|readonly)$ ]] && break
    warn "type full, safe or readonly"
  done
  ask PROTECTED       "Protected VMIDs, comma separated (this container's VMID at least)" ""
  ask SSH_ON          "Enable SSH for host shell and container commands? (yes/no)" "yes"
  while :; do
    ask_secret ADMIN_PW  "Admin password for approving Claude (12+ characters)"
    [[ ${#ADMIN_PW} -ge 12 ]] && break
    warn "too short"
  done
  ADMIN_HASH=$("$PREFIX/venv/bin/python" -c 'import sys; from pve_connector.auth import hash_password; print(hash_password(sys.argv[1]))' "$ADMIN_PW")
  unset ADMIN_PW

  umask 077
  cat >"$ENV_FILE" <<EOF
PVE_HOST=$PVE_HOST
PVE_PORT=8006
PVE_TOKEN_ID=$PVE_TOKEN_ID
PVE_TOKEN_SECRET=$PVE_TOKEN_SECRET
PVE_VERIFY_SSL=$PVE_VERIFY_SSL
PVE_SSH_ENABLED=$([[ $SSH_ON == y* ]] && echo true || echo false)
PVE_SSH_USER=root
PVE_SSH_KEY_PATH=$DATA_DIR/id_ed25519
CONNECTOR_PUBLIC_URL=${PUBLIC_URL%/}
CONNECTOR_BIND_HOST=127.0.0.1
CONNECTOR_PORT=8765
CONNECTOR_ADMIN_PASSWORD='$ADMIN_HASH'
CONNECTOR_DATA_DIR=$DATA_DIR
CONNECTOR_MODE=$MODE
CONNECTOR_PROTECTED_VMIDS=$PROTECTED
CONNECTOR_ALLOWED_VMIDS=
CONNECTOR_ALLOWED_REDIRECT_HOSTS=claude.ai,claude.com,localhost,127.0.0.1
CONNECTOR_LOG_LEVEL=INFO
EOF
fi
# Always enforce: secrets readable by root and the service only.
chown root:"$SVC_USER" "$ENV_FILE"; chmod 0640 "$ENV_FILE"

set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

if [[ "${PVE_SSH_ENABLED:-false}" == "true" && ! -f "$DATA_DIR/id_ed25519" ]]; then
  say "Creating the SSH key the connector uses to reach Proxmox"
  ssh-keygen -q -t ed25519 -N '' -C "pve-connector@$(hostname)" -f "$DATA_DIR/id_ed25519"
  chown "$SVC_USER:$SVC_USER" "$DATA_DIR/id_ed25519" "$DATA_DIR/id_ed25519.pub"
  SSH_KEY_CREATED=1
fi

say "Installing the systemd service"
cat >/etc/systemd/system/pve-connector.service <<'UNIT'
[Unit]
Description=Proxmox Claude Connector (MCP server)
Documentation=https://github.com/ahmedmotta/proxmox-claude-connector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pve-connector
Group=pve-connector
EnvironmentFile=/etc/pve-connector/env
Environment=PVE_CONNECTOR_ENV=/etc/pve-connector/env
ExecStart=/opt/pve-connector/venv/bin/pve-connector serve
Restart=always
RestartSec=3

# Hardening
NoNewPrivileges=true
UMask=0077
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/pve-connector
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictNamespaces=true
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable pve-connector >/dev/null 2>&1
systemctl restart pve-connector   # picks up an upgrade on re-runs
sleep 2
systemctl is-active --quiet pve-connector || { journalctl -u pve-connector -n 30 --no-pager; die "service failed to start"; }

if [[ -n "${CF_TUNNEL_TOKEN_FILE:-}" && -f "$CF_TUNNEL_TOKEN_FILE" ]]; then
  CF_TUNNEL_TOKEN=$(<"$CF_TUNNEL_TOKEN_FILE")
  rm -f "$CF_TUNNEL_TOKEN_FILE"
fi
if [[ -n "${CF_TUNNEL_TOKEN:-}" ]]; then
  say "Installing cloudflared and connecting the tunnel"
  install -d -m 0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    >/etc/apt/sources.list.d/cloudflared.list
  apt-get update -qq && apt-get install -y -qq cloudflared >/dev/null
  cloudflared service uninstall >/dev/null 2>&1 || true
  cloudflared service install "$CF_TUNNEL_TOKEN"
fi

say "Checking the connection to Proxmox"
sudo_check() { runuser -u "$SVC_USER" -- env PVE_CONNECTOR_ENV="$ENV_FILE" "$PREFIX/venv/bin/pve-connector" check; }
sudo_check || warn "the check reported problems (see above)"

echo
say "Done. The connector listens on http://127.0.0.1:${CONNECTOR_PORT:-8765}"
if [[ -n "${SSH_KEY_CREATED:-}" ]]; then
  echo
  echo "Add this public key to /root/.ssh/authorized_keys on the Proxmox host(s), then run: pve-connector check"
  cat "$DATA_DIR/id_ed25519.pub"
fi
echo
echo "Next: point your Cloudflare Tunnel public hostname at http://localhost:${CONNECTOR_PORT:-8765},"
echo "then in Claude add a custom connector with the URL: ${CONNECTOR_PUBLIC_URL}/mcp"
