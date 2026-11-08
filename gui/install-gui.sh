#!/usr/bin/env bash
# Adds the "Claude Connector" tab to the Proxmox VE web interface (Datacenter > Claude Connector).
#
#   pve-claude-gui install https://pve-mcp.example.com [--all-nodes]
#   pve-claude-gui remove [--all-nodes]
#   pve-claude-gui status
#   pve-claude-gui reapply          # run automatically after Proxmox package upgrades
#
# What it changes on the node:
#   /usr/share/pve-manager/js/claude-connector.js     the tab (served at /pve2/js/claude-connector.js)
#   /usr/share/pve-manager/index.html.tpl             one <script> line, marked "pve-claude-connector"
#   /etc/apt/apt.conf.d/99pve-claude-connector        re-adds that line after pve-manager upgrades
#   /usr/local/sbin/pve-claude-gui, /usr/local/share/pve-claude-connector/
# `remove` undoes all of it.
set -euo pipefail

REPO="ahmedmotta/proxmox-claude-connector"
RAW="https://raw.githubusercontent.com/${REPO}/main"
GUI_VERSION="0.1.0"
MANAGER_DIR="${PVE_MANAGER_DIR:-/usr/share/pve-manager}"
TEMPLATE="$MANAGER_DIR/index.html.tpl"
SERVED_JS="$MANAGER_DIR/js/claude-connector.js"
SHARE_DIR="${PCC_SHARE_DIR:-/usr/local/share/pve-claude-connector}"
SELF="${PCC_SELF:-/usr/local/sbin/pve-claude-gui}"
APT_HOOK="${PCC_APT_HOOK:-/etc/apt/apt.conf.d/99pve-claude-connector}"
MARKER="pve-claude-connector"
ANCHOR="/pve2/js/pvemanagerlib.js"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

restart_proxy() {
  # pveproxy indexes its static files at start, so a new file needs a restart.
  if [[ -z "${PCC_NO_RESTART:-}" ]] && command -v systemctl >/dev/null; then
    systemctl restart pveproxy
  fi
}

patch_template() {
  # Returns 0 when the template was changed.
  [[ -f "$TEMPLATE" ]] || die "$TEMPLATE not found - is this a Proxmox VE node?"
  grep -q "$MARKER" "$TEMPLATE" && return 1
  grep -q "$ANCHOR" "$TEMPLATE" || die "unexpected Proxmox GUI template: $ANCHOR not found"
  local line="    <script type=\"text/javascript\" src=\"/pve2/js/claude-connector.js?ver=${GUI_VERSION}\"></script><!-- ${MARKER} -->"
  python3 - "$TEMPLATE" "$ANCHOR" "$line" <<'PY'
import sys
path, anchor, new = sys.argv[1:4]
lines = open(path).read().split("\n")
out = []
for line in lines:
    out.append(line)
    if anchor in line:
        out.append(new)
open(path, "w").write("\n".join(out))
PY
  return 0
}

unpatch_template() {
  [[ -f "$TEMPLATE" ]] || return 1
  grep -q "$MARKER" "$TEMPLATE" || return 1
  sed -i "/$MARKER/d" "$TEMPLATE"
  return 0
}

fetch_js() {
  # Prefer a copy next to this script (git checkout), else download it.
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "$here/claude-connector.js" ]]; then
    cat "$here/claude-connector.js"
  else
    curl -fsSL "$RAW/gui/claude-connector.js"
  fi
}

other_nodes() {
  local me
  me=$(hostname)
  pvesh get /nodes --output-format json 2>/dev/null \
    | python3 -c 'import json,sys; [print(n["node"]) for n in json.load(sys.stdin) if n.get("status")=="online"]' \
    | grep -vx "$me" || true
}

on_other_nodes() {
  local node
  for node in $(other_nodes); do
    say "Node $node"
    ssh -o BatchMode=yes "root@$node" "mkdir -p /tmp/pcc-gui" \
      && scp -q -o BatchMode=yes "$SELF" "$SHARE_DIR/claude-connector.js.in" "root@$node:/tmp/pcc-gui/" \
      && ssh -o BatchMode=yes "root@$node" \
           "cp /tmp/pcc-gui/claude-connector.js.in /tmp/pcc-gui/claude-connector.js && bash /tmp/pcc-gui/pve-claude-gui $*; rm -rf /tmp/pcc-gui" \
      || warn "could not update node $node"
  done
}

cmd_install() {
  local url=${1:-} all=${2:-}
  [[ $url =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ || $url =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$ ]] \
    || die "give the connector's public URL, e.g. https://pve-mcp.example.com (no path, no trailing slash)"
  [[ $EUID -eq 0 ]] || die "run as root on a Proxmox node"

  say "Installing the Claude Connector tab ($GUI_VERSION) for $url"
  mkdir -p "$SHARE_DIR"
  fetch_js >"$SHARE_DIR/claude-connector.js.in"
  grep -q "__CONNECTOR_URL__" "$SHARE_DIR/claude-connector.js.in" || die "downloaded tab file looks wrong"
  printf '%s\n' "$url" >"$SHARE_DIR/connector-url"
  render_js
  if [[ ! -f "${BASH_SOURCE[0]}" ]]; then
    curl -fsSL "$RAW/gui/install-gui.sh" -o "$SELF" && chmod 0755 "$SELF"
  elif [[ "$(readlink -f "${BASH_SOURCE[0]}")" != "$(readlink -f "$SELF" 2>/dev/null || true)" ]]; then
    install -m 0755 "${BASH_SOURCE[0]}" "$SELF"
  fi
  cat >"$APT_HOOK" <<EOF
// Re-adds the Claude Connector tab after Proxmox web interface upgrades. Remove with: pve-claude-gui remove
DPkg::Post-Invoke { "if [ -x $SELF ]; then $SELF reapply >/dev/null 2>&1 || true; fi"; };
EOF
  unpatch_template || true  # refresh the line so a new ?ver= busts browser caches
  patch_template || true
  restart_proxy
  say "Done. Reload the Proxmox page (Ctrl+F5) and open Datacenter > Claude Connector."
  [[ $all == --all-nodes ]] && on_other_nodes install "$url"
  return 0
}

render_js() {
  local url
  url=$(<"$SHARE_DIR/connector-url")
  python3 - "$SHARE_DIR/claude-connector.js.in" "$SERVED_JS" "$url" "$GUI_VERSION" <<'PY'
import sys
src, dst, url, version = sys.argv[1:5]
text = open(src).read().replace("__CONNECTOR_URL__", url).replace("__GUI_VERSION__", version)
open(dst, "w").write(text)
PY
  chmod 0644 "$SERVED_JS"
}

cmd_reapply() {
  [[ -f "$SHARE_DIR/connector-url" && -f "$SHARE_DIR/claude-connector.js.in" ]] || exit 0
  local changed=0
  if [[ ! -f "$SERVED_JS" ]]; then render_js; changed=1; fi
  if patch_template; then changed=1; fi
  if [[ $changed -eq 1 ]]; then restart_proxy; fi
  return 0
}

cmd_remove() {
  local all=${1:-}
  [[ $EUID -eq 0 ]] || die "run as root on a Proxmox node"
  [[ $all == --all-nodes ]] && on_other_nodes remove
  say "Removing the Claude Connector tab"
  unpatch_template || true
  rm -f "$SERVED_JS" "$APT_HOOK"
  rm -rf "$SHARE_DIR"
  restart_proxy
  say "Removed. Reload the Proxmox page."
  rm -f "$SELF"  # last: safe even when this is the running script
}

cmd_status() {
  printf 'tab file:      %s\n' "$([[ -f $SERVED_JS ]] && echo present || echo missing)"
  printf 'template line: %s\n' "$(grep -q "$MARKER" "$TEMPLATE" 2>/dev/null && echo present || echo missing)"
  printf 'upgrade hook:  %s\n' "$([[ -f $APT_HOOK ]] && echo present || echo missing)"
  printf 'connector:     %s\n' "$(cat "$SHARE_DIR/connector-url" 2>/dev/null || echo 'not set')"
}

case "${1:-}" in
  install) shift; cmd_install "$@" ;;
  remove)  shift; cmd_remove "$@" ;;
  reapply) cmd_reapply ;;
  status)  cmd_status ;;
  *) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
