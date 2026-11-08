#!/usr/bin/env bash
# Proxmox Claude Connector - one-command setup, run as root on a Proxmox VE node:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/ahmedmotta/proxmox-claude-connector/main/deploy/proxmox-setup.sh)"
#
# It will:
#   0. ask a few questions (public URL, tunnel token, password, permission mode)
#   1. create a full-access API token (root@pam!claude)
#   2. create a small Debian container for the connector (1 core, 512 MB, 4 GB)
#   3. install the connector inside it as a service, plus cloudflared if you give a tunnel token
#   4. give the connector SSH access to this node (host shell + container commands)
#   5. protect the connector's own container from being stopped or deleted by Claude
#   6. add the "Claude Connector" tab to the Proxmox web interface on every node
set -euo pipefail

REPO="ahmedmotta/proxmox-claude-connector"
RAW="https://raw.githubusercontent.com/${REPO}/main"
TOKEN_USER="root@pam"
TOKEN_NAME="claude"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }
ask()  { local __v=$1 __q=$2 __d=${3:-} __a; read -r -p "$__q${__d:+ [$__d]}: " __a </dev/tty || true; printf -v "$__v" '%s' "${__a:-$__d}"; }
ask_secret() { local __v=$1 __q=$2 __a; read -r -s -p "$__q: " __a </dev/tty || true; echo; printf -v "$__v" '%s' "$__a"; }

[[ $EUID -eq 0 ]] || die "run as root on a Proxmox node"
command -v pveversion >/dev/null || die "this is not a Proxmox VE node"

echo
echo "Proxmox Claude Connector setup  ($(pveversion))"
echo

# ---- questions -------------------------------------------------------------------------
ask PUBLIC_URL "Public HTTPS URL Claude will use (e.g. https://pve-mcp.example.com)" ""
[[ $PUBLIC_URL == https://* ]] || die "the URL must start with https://"
PUBLIC_URL=${PUBLIC_URL%/}
ask_secret CF_TUNNEL_TOKEN "Cloudflare Tunnel token (Enter to skip and set it up later)"
while :; do
  ask_secret ADMIN_PW "Admin password to approve Claude's connection (12+ characters)"
  [[ ${#ADMIN_PW} -ge 12 ]] || { warn "too short"; continue; }
  ask_secret ADMIN_PW2 "Again"
  [[ $ADMIN_PW == "$ADMIN_PW2" ]] && break
  warn "passwords do not match"
done
while :; do
  ask MODE "Permission mode: full / safe / readonly" "full"
  [[ $MODE =~ ^(full|safe|readonly)$ ]] && break
  warn "type full, safe or readonly"
done
ask STORAGE "Storage for the connector container" "local-lvm"
ask BRIDGE "Network bridge" "vmbr0"
ask CT_IP "Container IP (dhcp, or e.g. 192.168.1.50/24,gw=192.168.1.1)" "dhcp"
ask EXTRA_PROTECTED "Other VMIDs Claude must never stop/delete (comma separated, optional)" ""

HOST_IP=$(ip -4 -o addr show "$BRIDGE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
[[ -n $HOST_IP ]] || HOST_IP=$(hostname -I | awk '{print $1}')
say "This node is reachable from the container at $HOST_IP"

# ---- API token -------------------------------------------------------------------------
if pveum user token list "$TOKEN_USER" --output-format json 2>/dev/null \
   | python3 -c 'import json,sys; sys.exit(0 if any(t.get("tokenid")==sys.argv[1] for t in json.load(sys.stdin)) else 1)' "$TOKEN_NAME"; then
  warn "API token $TOKEN_USER!$TOKEN_NAME already exists; it will be replaced"
  pveum user token remove "$TOKEN_USER" "$TOKEN_NAME"
fi
say "Creating API token $TOKEN_USER!$TOKEN_NAME (full access, no privilege separation)"
TOKEN_SECRET=$(pveum user token add "$TOKEN_USER" "$TOKEN_NAME" --privsep 0 \
  --comment "Claude connector" --output-format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])')

# ---- certificate check -----------------------------------------------------------------
VERIFY=true
CODE=$(curl -sS --max-time 5 --cacert /etc/pve/pve-root-ca.pem -o /dev/null -w '%{http_code}' \
  "https://$HOST_IP:8006/api2/json/version" 2>/dev/null || true)
if [[ -z $CODE || $CODE == 000 ]]; then
  warn "the node certificate does not cover $HOST_IP; TLS verification will be off (LAN only)"
  VERIFY=false
fi

# ---- container -------------------------------------------------------------------------
say "Finding a Debian container template"
pveam update >/dev/null 2>&1 || warn "pveam update failed; using what is already known"
# Proxmox VE 8 creates Debian 12 containers and VE 9 Debian 13. Proxmox VE 7 rejects every Debian
# 12.x point release ("unsupported debian version '12.12'") and Debian 11 is end-of-life, so on VE 7
# the container runs Debian 12 as "unmanaged" and this script configures its network itself.
PVE_MAJOR=$(pveversion | sed -n 's#^pve-manager/\([0-9]*\)\..*#\1#p')
PVE_MAJOR=${PVE_MAJOR:-0}
OSTYPE_ARGS=()
case "$PVE_MAJOR" in
  7) DEBIAN=12; OSTYPE_ARGS=(--ostype unmanaged) ;;
  8) DEBIAN=12 ;;
  *) DEBIAN=13 ;;
esac
pick_template() { pveam available --section system | awk -v d="debian-$1-standard" '$2 ~ "^"d {print $2}' | sort -V | tail -1; }
TEMPLATE=$(pick_template "$DEBIAN")
[[ -n $TEMPLATE || $DEBIAN -ne 13 ]] || TEMPLATE=$(pick_template 12)
[[ -n $TEMPLATE ]] || die "no Debian $DEBIAN container template found in the catalog"
say "Proxmox VE $PVE_MAJOR detected; using $TEMPLATE"
if [[ $PVE_MAJOR -lt 8 ]]; then
  warn "Proxmox VE $PVE_MAJOR no longer receives security updates; upgrading to a current release is recommended"
fi
if ! pveam list local | grep -q "$TEMPLATE"; then
  say "Downloading $TEMPLATE"
  pveam download local "$TEMPLATE"
fi

# A previous, unfinished run may have left a connector container behind.
OLD_CT=$(pct list | awk '$NF == "pve-connector" {print $1}' | head -1)
if [[ -n $OLD_CT ]]; then
  ask REPLACE "Container $OLD_CT (pve-connector) already exists. Remove it and start fresh? (yes/no)" "yes"
  [[ $REPLACE == y* ]] || die "stopped; remove or rename container $OLD_CT first"
  say "Removing container $OLD_CT"
  pct stop "$OLD_CT" >/dev/null 2>&1 || true
  pct destroy "$OLD_CT" --purge >/dev/null
fi

CTID=$(pvesh get /cluster/nextid)
say "Creating container $CTID (pve-connector)"
pct create "$CTID" "local:vztmpl/$TEMPLATE" \
  --hostname pve-connector --description "Proxmox Claude Connector - https://github.com/$REPO" \
  --tags claude --cores 1 --memory 512 --swap 256 --rootfs "$STORAGE:4" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=$CT_IP" --unprivileged 1 --features nesting=1 \
  --onboot 1 "${OSTYPE_ARGS[@]}" >/dev/null
pct start "$CTID"

if [[ ${#OSTYPE_ARGS[@]} -gt 0 ]]; then
  say "Configuring the container network (unmanaged container on Proxmox VE 7)"
  NETDIR=$(mktemp -d)
  {
    printf 'auto lo\niface lo inet loopback\n\nauto eth0\n'
    if [[ $CT_IP == dhcp ]]; then
      printf 'iface eth0 inet dhcp\n'
    else
      GW=$(sed -n 's/.*gw=\([^,]*\).*/\1/p' <<<"$CT_IP")
      printf 'iface eth0 inet static\n    address %s\n' "${CT_IP%%,*}"
      [[ -n $GW ]] && printf '    gateway %s\n' "$GW"
    fi
  } >"$NETDIR/interfaces"
  printf 'pve-connector\n' >"$NETDIR/hostname"
  printf '127.0.0.1 localhost\n127.0.1.1 pve-connector\n' >"$NETDIR/hosts"
  sleep 3
  pct push "$CTID" "$NETDIR/interfaces" /etc/network/interfaces --perms 0644
  pct push "$CTID" "$NETDIR/hostname" /etc/hostname --perms 0644
  pct push "$CTID" "$NETDIR/hosts" /etc/hosts --perms 0644
  [[ $CT_IP == dhcp ]] || pct push "$CTID" /etc/resolv.conf /etc/resolv.conf --perms 0644
  rm -rf "$NETDIR"
  pct exec "$CTID" -- bash -c 'hostname pve-connector; systemctl restart networking || ifup eth0' || true
fi

say "Waiting for the container network"
for _ in $(seq 1 60); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done
pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 || die "container $CTID has no internet access"

# ---- settings file ---------------------------------------------------------------------
ADMIN_HASH=$(python3 - "$ADMIN_PW" <<'PY'
import hashlib, secrets, sys
salt = secrets.token_bytes(16)
digest = hashlib.scrypt(sys.argv[1].encode(), salt=salt, n=2**14, r=8, p=1)
print(f"scrypt${salt.hex()}${digest.hex()}")
PY
)
unset ADMIN_PW ADMIN_PW2
PROTECTED="$CTID${EXTRA_PROTECTED:+,$EXTRA_PROTECTED}"

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT; chmod 700 "$TMP"
cat >"$TMP/env" <<EOF
PVE_HOST=$HOST_IP
PVE_PORT=8006
PVE_TOKEN_ID=$TOKEN_USER!$TOKEN_NAME
PVE_TOKEN_SECRET=$TOKEN_SECRET
PVE_VERIFY_SSL=$VERIFY
$([[ $VERIFY == true ]] && echo "PVE_CA_CERT=/etc/pve-connector/pve-root-ca.pem")
PVE_SSH_ENABLED=true
PVE_SSH_HOST=$HOST_IP
PVE_SSH_USER=root
PVE_SSH_KEY_PATH=/var/lib/pve-connector/id_ed25519
CONNECTOR_PUBLIC_URL=$PUBLIC_URL
CONNECTOR_BIND_HOST=127.0.0.1
CONNECTOR_PORT=8765
CONNECTOR_ADMIN_PASSWORD='$ADMIN_HASH'
CONNECTOR_DATA_DIR=/var/lib/pve-connector
CONNECTOR_MODE=$MODE
CONNECTOR_PROTECTED_VMIDS=$PROTECTED
CONNECTOR_ALLOWED_VMIDS=
CONNECTOR_ALLOWED_REDIRECT_HOSTS=claude.ai,claude.com,localhost,127.0.0.1
CONNECTOR_LOG_LEVEL=INFO
EOF
pct exec "$CTID" -- mkdir -p /etc/pve-connector
pct push "$CTID" "$TMP/env" /etc/pve-connector/env --perms 0600
pct push "$CTID" /etc/pve/pve-root-ca.pem /etc/pve-connector/pve-root-ca.pem --perms 0644

# ---- install inside the container ------------------------------------------------------
say "Installing the connector inside container $CTID (this takes a minute)"
pct exec "$CTID" -- bash -c "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates >/dev/null"
if [[ -n $CF_TUNNEL_TOKEN ]]; then
  ( umask 077; printf '%s' "$CF_TUNNEL_TOKEN" >"$TMP/cf" )
  pct push "$CTID" "$TMP/cf" /root/.cf-tunnel-token --perms 0600
fi
pct exec "$CTID" -- env NONINTERACTIVE=1 CF_TUNNEL_TOKEN_FILE=/root/.cf-tunnel-token \
  bash -c "curl -fsSL '$RAW/deploy/install.sh' | bash" || die "installation inside the container failed"

# ---- SSH trust -------------------------------------------------------------------------
say "Authorizing the connector's SSH key on this node"
PUBKEY=$(pct exec "$CTID" -- cat /var/lib/pve-connector/id_ed25519.pub)
# With a static container IP, the key only works from that address.
if [[ $CT_IP != dhcp ]]; then
  PUBKEY="from=\"${CT_IP%%/*}\" $PUBKEY"
fi
AUTH=/root/.ssh/authorized_keys
mkdir -p /root/.ssh
# On Proxmox this file is shared by every node in the cluster (it links to /etc/pve/priv), so the
# connector gets root SSH on all nodes - which is what host_shell on other nodes needs.
grep -qF "$PUBKEY" "$AUTH" 2>/dev/null || echo "$PUBKEY" >>"$AUTH"
pct exec "$CTID" -- systemctl restart pve-connector
sleep 2
pct exec "$CTID" -- runuser -u pve-connector -- env PVE_CONNECTOR_ENV=/etc/pve-connector/env \
  /opt/pve-connector/venv/bin/pve-connector check || warn "the check reported problems (see above)"

say "Adding the Claude Connector tab to the Proxmox web interface (all nodes)"
if curl -fsSL "$RAW/gui/install-gui.sh" -o "$TMP/install-gui.sh"; then
  bash "$TMP/install-gui.sh" install "$PUBLIC_URL" --all-nodes || warn "the web interface tab could not be installed"
else
  warn "could not download the web interface tab installer"
fi

cat <<EOF

============================================================
 Connector installed in container $CTID (protected from Claude).

 1) Cloudflare Zero Trust -> Networks -> Tunnels -> your tunnel
    -> Public Hostname: $(echo "$PUBLIC_URL" | sed 's#https://##')
       Service: HTTP  localhost:8765
$( [[ -z $CF_TUNNEL_TOKEN ]] && echo "    No tunnel token was given. To add it later, inside the container run:
      read -rs CF_TUNNEL_TOKEN; export CF_TUNNEL_TOKEN NONINTERACTIVE=1
      curl -fsSL $RAW/deploy/install.sh | bash" )

 2) Proxmox web interface -> Datacenter -> Claude Connector
    (reload the page first) to see status, change permissions,
    pause Claude, and disconnect sessions.

 3) Claude -> Settings -> Connectors -> Add custom connector
       URL: $PUBLIC_URL/mcp
    Approve with the admin password you chose.

 Useful (inside the container: pct enter $CTID):
    pve-connector check        test everything
    pve-connector sessions     who is connected
    pve-connector revoke-all   disconnect Claude
    journalctl -u pve-connector -f
============================================================
EOF
