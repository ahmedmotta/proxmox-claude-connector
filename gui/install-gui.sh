#!/usr/bin/env bash
# Adds the "Claude Connector" tab (Datacenter > Claude Connector) and the brand theme to the
# Proxmox VE web interface. Run `pve-claude-gui` without arguments for the commands.
#
# What it changes on the node:
#   /usr/share/pve-manager/js/claude-connector.js     the tab (served at /pve2/js/claude-connector.js)
#   /usr/share/pve-manager/js/claude-theme.js         the theme's top bar, switcher and chart colors
#   /usr/share/javascript/proxmox-widget-toolkit/themes/theme-pcc-*.css, pcc-*
#                                                     the theme itself (served at /pwt/themes/)
#   /usr/share/pve-manager/index.html.tpl             <script> lines, marked "pve-claude-connector"
#   /etc/apt/apt.conf.d/99pve-claude-connector        puts all of it back after Proxmox upgrades
#   /usr/local/sbin/pve-claude-gui, /usr/local/share/pve-claude-connector/
# `remove` undoes all of it. Nothing is restarted: pveproxy serves new files and reads its page
# template on every request, so open consoles and shells keep running.
set -euo pipefail

REPO="ahmedmotta/proxmox-claude-connector"
RAW="${PCC_RAW:-https://raw.githubusercontent.com/${REPO}/${PCC_REF:-main}}"   # PCC_REF pins a branch, tag or commit
GUI_VERSION="0.3.0"
MANAGER_DIR="${PVE_MANAGER_DIR:-/usr/share/pve-manager}"
THEMES_DIR="${PWT_THEMES_DIR:-/usr/share/javascript/proxmox-widget-toolkit/themes}"
TEMPLATE="$MANAGER_DIR/index.html.tpl"
SERVED_JS="$MANAGER_DIR/js/claude-connector.js"
THEME_JS="$MANAGER_DIR/js/claude-theme.js"
SHARE_DIR="${PCC_SHARE_DIR:-/usr/local/share/pve-claude-connector}"
THEME_DIR="$SHARE_DIR/theme"   # checked copy of gui/theme/dist, kept for `reapply` and other nodes
SELF="${PCC_SELF:-/usr/local/sbin/pve-claude-gui}"
APT_HOOK="${PCC_APT_HOOK:-/etc/apt/apt.conf.d/99pve-claude-connector}"
MARKER="pve-claude-connector"
ANCHOR="/pve2/js/pvemanagerlib.js"
SAFE_NAME='^[A-Za-z0-9][A-Za-z0-9._-]*$'

# Where the tab and theme files come from, in this order:
#   --from DIR          the files another node of the cluster sent along (see on_other_nodes)
#   a git checkout      when this script runs from gui/ in a clone of the repository
#   the installed copy  when this is the installed pve-claude-gui: the files it installed, same version
#   GitHub ($RAW)       otherwise, e.g. piped from curl. Files that merely sit next to the script are
#                       never used: in a shared directory such as /tmp anyone could have put them there.
private_dir() {
  # A directory nobody but its owner can write to.
  [[ -d $1 ]] && (( (8#$(stat -c %a "$1") & 8#022) == 0 ))
}

HERE="" SRC="" INSTALLED=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -e "$HERE/../.git" && -f "$HERE/claude-connector.js" && -f "$HERE/theme/dist/SHA256SUMS" ]] \
    && private_dir "$HERE"; then
    SRC=$HERE
  fi
  if [[ "$(readlink -f "${BASH_SOURCE[0]}")" == "$(readlink -f "$SELF" 2>/dev/null || true)" ]]; then
    INSTALLED=1
  fi
fi

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
pve-claude-gui install https://pve-mcp.example.com [--all-nodes] [--theme dark|light|none] [--no-theme]
               [--brand "Name"] [--brand-url https://example.com] [--brand-logo logo.png]
pve-claude-gui theme dark|light|none|off [--all-nodes]
pve-claude-gui remove [--all-nodes]
pve-claude-gui status
pve-claude-gui reapply          # runs by itself after Proxmox package upgrades

--theme picks the default theme: every browser switches to it once (default: dark). "none" installs
the themes without switching anyone; --no-theme and "theme off" keep the stock look. Everyone can
change their own theme with the switcher in the top bar.
--brand, --brand-url and --brand-logo (a square PNG) put your own name, link and logo in the top bar;
they are kept for later installs, and "default" puts the project's own back.
EOF
}

use_source() {
  # --from DIR: only a directory that belongs to root and nobody else can write to.
  local dir=$1
  [[ -d $dir && -O $dir ]] || die "--from: $dir is not a directory owned by root"
  private_dir "$dir" || die "--from: $dir must not be writable by others"
  SRC=$(cd "$dir" && pwd)
}

file_version() {
  # Short content hash, so browsers fetch a file again whenever it changes.
  sha256sum "$1" | cut -c1-10
}

# ---- the GUI template -----------------------------------------------------------------------

template_lines() {
  # The lines this node should carry right after the Proxmox GUI script, in order.
  printf '    <script type="text/javascript" src="/pve2/js/claude-connector.js?ver=%s-%s"></script><!-- %s -->' \
    "$GUI_VERSION" "$(file_version "$SERVED_JS")" "$MARKER"
  if [[ -f "$THEME_JS" ]]; then
    printf '\n    <script type="text/javascript" src="/pve2/js/claude-theme.js?ver=%s-%s"></script><!-- %s -->' \
      "$GUI_VERSION" "$(file_version "$THEME_JS")" "$MARKER"
  fi
}

patch_template() {
  # Makes the template carry exactly template_lines. Returns 0 when it changed.
  [[ -f "$TEMPLATE" ]] || die "$TEMPLATE not found - is this a Proxmox VE node?"
  grep -qF "$ANCHOR" "$TEMPLATE" || die "unexpected Proxmox GUI template: $ANCHOR not found"
  python3 - "$TEMPLATE" "$ANCHOR" "$MARKER" "$(template_lines)" <<'PY'
import os, shutil, sys
path, anchor, marker, wanted = sys.argv[1:5]
text = open(path).read()
out = []
for line in text.split("\n"):
    if marker in line:
        continue
    out.append(line)
    if anchor in line:
        out.extend(wanted.split("\n"))
new = "\n".join(out)
if new == text:
    sys.exit(1)
tmp = path + ".pcc-new"
with open(tmp, "w") as f:
    f.write(new)
shutil.copymode(path, tmp)
os.replace(tmp, path)  # pveproxy never sees a half-written template
PY
}

unpatch_template() {
  [[ -f "$TEMPLATE" ]] || return 1
  grep -qF "$MARKER" "$TEMPLATE" || return 1
  sed -i "/$MARKER/d" "$TEMPLATE"
  return 0
}

# ---- the tab ----------------------------------------------------------------------------------

fetch_js() {
  if [[ -n $SRC ]]; then
    cat "$SRC/claude-connector.js"
  elif [[ -n $INSTALLED && -f "$SHARE_DIR/claude-connector.js.in" ]]; then
    cat "$SHARE_DIR/claude-connector.js.in"
  else
    curl -fsSL "$RAW/gui/claude-connector.js"
  fi
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

# ---- the theme ----------------------------------------------------------------------------------

theme_name() {
  case ${1:-} in
    dark) echo pcc-dark ;;
    light) echo pcc-light ;;
    none | off) echo "$1" ;;
    *) die "unknown theme '${1:-}' (use dark, light, none or off)" ;;
  esac
}

theme_supported() {
  # Proxmox VE 7.2 and later load /pwt/themes/theme-<name>.css from the theme cookie.
  [[ -d "$THEMES_DIR" ]] && grep -qF 'theme-[% theme %].css' "$TEMPLATE" 2>/dev/null
}

fetch_theme() {
  # Fills $THEME_DIR with the theme files, checked against their SHA256SUMS before anything is used.
  local tmp name
  if [[ -z $SRC && -n $INSTALLED && -f "$THEME_DIR/SHA256SUMS" ]]; then
    return 0  # the installed command keeps using the theme files it installed
  fi
  tmp=$(mktemp -d)
  if [[ -n $SRC ]]; then
    [[ -f "$SRC/theme/dist/SHA256SUMS" ]] || { rm -rf "$tmp"; die "no theme files in $SRC/theme/dist"; }
    cp -a "$SRC/theme/dist/." "$tmp/"
  else
    curl -fsSL "$RAW/gui/theme/dist/SHA256SUMS" -o "$tmp/SHA256SUMS" || { rm -rf "$tmp"; die "could not download the theme"; }
    while read -r _ name; do
      [[ $name =~ $SAFE_NAME ]] || { rm -rf "$tmp"; die "unexpected file name in the theme checksums: $name"; }
      curl -fsSL "$RAW/gui/theme/dist/$name" -o "$tmp/$name" || { rm -rf "$tmp"; die "could not download the theme file $name"; }
    done <"$tmp/SHA256SUMS"
  fi
  if ! (cd "$tmp" && sha256sum --quiet --strict -c SHA256SUMS); then
    rm -rf "$tmp"
    die "the theme files failed their checksum - nothing was changed"
  fi
  mkdir -p "$SHARE_DIR"
  rm -rf "$THEME_DIR"
  mv "$tmp" "$THEME_DIR"
  chmod 0755 "$THEME_DIR"
}

render_theme_js() {
  local name="" url="" logo
  [[ -f "$SHARE_DIR/brand-name" ]] && name=$(<"$SHARE_DIR/brand-name")
  [[ -f "$SHARE_DIR/brand-url" ]] && url=$(<"$SHARE_DIR/brand-url")
  logo="/pwt/themes/pcc-logo.png?v=$(file_version "$THEMES_DIR/pcc-logo.png")"
  python3 - "$THEME_DIR/claude-theme.js" "$THEME_JS" "$GUI_VERSION" "$(<"$SHARE_DIR/theme-default")" \
    "$name" "$url" "$logo" <<'PY'
import json, sys
src, dst, version, default, name, url, logo = sys.argv[1:8]
text = open(src).read().replace("__GUI_VERSION__", version).replace("__DEFAULT_THEME__", default)
for key, value in (("NAME", name), ("URL", url), ("LOGO", logo)):
    if value:  # a JSON string is a safe JavaScript string literal
        text = text.replace("'__BRAND_%s__'" % key, json.dumps(value))
open(dst, "w").write(text)
PY
  chmod 0644 "$THEME_JS"
}

set_brand() {
  # set_brand name|url|logo VALUE - checks and stores one brand override; "default" removes it.
  local key=$1 value=$2 re
  if [[ $value == default ]]; then
    if [[ $key == logo ]]; then rm -f "$SHARE_DIR/brand-logo.png"; else rm -f "$SHARE_DIR/brand-$key"; fi
    return 0
  fi
  mkdir -p "$SHARE_DIR"
  case $key in
    name)
      [[ -n $value && ${#value} -le 40 && $value != *[[:cntrl:]]* && $value != *[\<\>]* ]] \
        || die "--brand: 1 to 40 characters, no < or >"
      printf '%s\n' "$value" >"$SHARE_DIR/brand-name"
      ;;
    url)
      re='^https?://[A-Za-z0-9._~:/?#@!$&()*+,;=%-]+$'
      [[ $value =~ $re ]] || die "--brand-url: give an http(s) address"
      printf '%s\n' "$value" >"$SHARE_DIR/brand-url"
      ;;
    logo)
      [[ -f $value ]] || die "--brand-logo: $value not found"
      [[ "$(head -c 8 "$value" | od -An -tx1 | tr -d ' \n')" == 89504e470d0a1a0a ]] || die "--brand-logo: the logo must be a PNG"
      [[ $(stat -c %s "$value") -le 524288 ]] || die "--brand-logo: keep the logo under 512 KB"
      install -m 0644 "$value" "$SHARE_DIR/brand-logo.png.new"
      mv "$SHARE_DIR/brand-logo.png.new" "$SHARE_DIR/brand-logo.png"
      ;;
  esac
}

theme_file_source() {
  # The file that belongs at $THEMES_DIR/<name>: a custom logo replaces the theme's own.
  if [[ $1 == pcc-logo.png && -f "$SHARE_DIR/brand-logo.png" ]]; then
    echo "$SHARE_DIR/brand-logo.png"
  else
    echo "$THEME_DIR/$1"
  fi
}

remove_theme_file() {
  if [[ $1 =~ $SAFE_NAME ]]; then
    rm -f "$THEMES_DIR/$1"
  fi
}

install_theme() {
  # Copies the checked theme files into place and records them for `remove`.
  local name
  : >"$SHARE_DIR/theme-files.new"
  while read -r _ name; do
    [[ $name =~ $SAFE_NAME ]] || die "unexpected file name in the theme checksums: $name"
    case $name in
      claude-theme.js | SHA256SUMS) ;;
      *)
        install -m 0644 "$(theme_file_source "$name")" "$THEMES_DIR/$name"
        printf '%s\n' "$name" >>"$SHARE_DIR/theme-files.new"
        ;;
    esac
  done <"$THEME_DIR/SHA256SUMS"
  if [[ -f "$SHARE_DIR/theme-files" ]]; then  # files an older version shipped but this one does not
    while read -r name; do
      grep -qxF "$name" "$SHARE_DIR/theme-files.new" || remove_theme_file "$name"
    done <"$SHARE_DIR/theme-files"
  fi
  mv "$SHARE_DIR/theme-files.new" "$SHARE_DIR/theme-files"
  render_theme_js
}

remove_theme() {
  local name
  if [[ -f "$SHARE_DIR/theme-files" ]]; then
    while read -r name; do remove_theme_file "$name"; done <"$SHARE_DIR/theme-files"
  fi
  rm -f "$THEME_JS" "$SHARE_DIR/theme-files" "$SHARE_DIR/theme-default"
  rm -rf "$THEME_DIR"
}

apply_theme() {
  # apply_theme pcc-dark|pcc-light|none|off
  local mode=$1
  if [[ $mode == off ]]; then
    remove_theme
    return 0
  fi
  if ! theme_supported; then
    warn "this Proxmox VE version cannot load themes (7.2 or later needed) - the theme was skipped"
    remove_theme
    return 0
  fi
  fetch_theme
  printf '%s\n' "$mode" >"$SHARE_DIR/theme-default"
  install_theme
}

theme_summary() {
  local mode
  [[ -f "$SHARE_DIR/theme-default" ]] || { echo "off (stock look)"; return 0; }
  mode=$(<"$SHARE_DIR/theme-default")
  case $mode in
    none) echo "installed; nobody is switched to it" ;;
    *) echo "${mode#pcc-} by default (every browser switches to it once)" ;;
  esac
}

# ---- other cluster nodes ----------------------------------------------------------------------------

other_nodes() {
  local me
  me=$(hostname)
  { pvesh get /nodes --output-format json 2>/dev/null || true; } | python3 -c '
import json, sys
try:
    nodes = json.load(sys.stdin)
except ValueError:  # not a cluster member, or pvesh is unavailable
    nodes = []
for node in nodes:
    if node.get("status") == "online":
        print(node["node"])
' | grep -vx "$me" || true
}

brand_args() {
  # This node's brand as install options for another node; a custom logo is sent as a file.
  printf '%s\0' --brand "$(cat "$SHARE_DIR/brand-name" 2>/dev/null || echo default)"
  printf '%s\0' --brand-url "$(cat "$SHARE_DIR/brand-url" 2>/dev/null || echo default)"
  if [[ ! -f "$SHARE_DIR/brand-logo.png" ]]; then printf '%s\0' --brand-logo default; fi
}

on_other_nodes() {
  # Runs this script with the given arguments on every other online node, with this node's files.
  local node dir qdir extra ssh_opts=(-o BatchMode=yes)
  for node in $(other_nodes); do
    say "Node $node"
    dir=$(ssh "${ssh_opts[@]}" "root@$node" 'mktemp -d /tmp/pcc-gui.XXXXXX' | tail -n 1) || dir=""
    if [[ ! $dir =~ ^/tmp/pcc-gui\.[A-Za-z0-9]+$ ]]; then
      warn "could not reach node $node"
      continue
    fi
    qdir=$(printf '%q' "$dir")
    extra=()
    if [[ $1 != remove ]]; then extra=(--from "$dir"); fi
    if [[ $1 == install && -f "$SHARE_DIR/brand-logo.png" ]]; then extra+=(--brand-logo "$dir/brand-logo.png"); fi
    if scp -q "${ssh_opts[@]}" "$SELF" "root@$node:$dir/pve-claude-gui" \
      && { [[ ! -f "$SHARE_DIR/claude-connector.js.in" ]] \
        || scp -q "${ssh_opts[@]}" "$SHARE_DIR/claude-connector.js.in" "root@$node:$dir/claude-connector.js"; } \
      && { [[ ! -f "$THEME_DIR/SHA256SUMS" ]] \
        || { ssh "${ssh_opts[@]}" "root@$node" "mkdir $qdir/theme" \
          && scp -q -r "${ssh_opts[@]}" "$THEME_DIR" "root@$node:$dir/theme/dist"; }; } \
      && { [[ ! -f "$SHARE_DIR/brand-logo.png" ]] \
        || scp -q "${ssh_opts[@]}" "$SHARE_DIR/brand-logo.png" "root@$node:$dir/brand-logo.png"; } \
      && ssh "${ssh_opts[@]}" "root@$node" "bash $qdir/pve-claude-gui $(printf '%q ' "$@" "${extra[@]}")"; then
      :
    else
      warn "could not update node $node"
    fi
    ssh "${ssh_opts[@]}" "root@$node" "rm -rf $qdir" || true
  done
}

# ---- commands ------------------------------------------------------------------------------------------

install_self() {
  if [[ -z $HERE ]]; then
    curl -fsSL "$RAW/gui/install-gui.sh" -o "$SELF.new" && chmod 0755 "$SELF.new" && mv "$SELF.new" "$SELF"
  elif [[ -z $INSTALLED ]]; then
    install -m 0755 "${BASH_SOURCE[0]}" "$SELF"
  fi
}

cmd_install() {
  local url="" all="" theme="" brand=()
  while [[ $# -gt 0 ]]; do
    case $1 in
      --all-nodes) all=--all-nodes ;;
      --no-theme) theme=off ;;
      --theme) shift; theme=$(theme_name "${1:-}") ;;
      --theme=*) theme=$(theme_name "${1#--theme=}") ;;
      --brand | --brand-url | --brand-logo)
        [[ $# -ge 2 ]] || die "$1 needs a value"
        brand+=("$1" "$2"); shift ;;
      --from)
        [[ $# -ge 2 ]] || die "--from needs a directory"
        use_source "$2"; shift ;;
      -*) die "unknown option: $1" ;;
      *) [[ -z $url ]] || die "unexpected argument: $1"; url=$1 ;;
    esac
    shift
  done
  [[ $url =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ || $url =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$ ]] \
    || die "give the connector's public URL, e.g. https://pve-mcp.example.com (no path, no trailing slash)"
  [[ $EUID -eq 0 ]] || die "run as root on a Proxmox node"

  if [[ -z $theme ]]; then  # keep what this node was set to before; dark for new installs
    theme=$(cat "$SHARE_DIR/theme-choice" 2>/dev/null || true)
    [[ $theme =~ ^(pcc-dark|pcc-light|none|off)$ ]] || theme=pcc-dark
  fi

  say "Installing the Claude Connector tab ($GUI_VERSION) for $url"
  mkdir -p "$SHARE_DIR"
  local i
  for ((i = 0; i < ${#brand[@]}; i += 2)); do
    case ${brand[i]} in
      --brand) set_brand name "${brand[i + 1]}" ;;
      --brand-url) set_brand url "${brand[i + 1]}" ;;
      --brand-logo) set_brand logo "${brand[i + 1]}" ;;
    esac
  done
  fetch_js >"$SHARE_DIR/claude-connector.js.in.new"
  grep -q "__CONNECTOR_URL__" "$SHARE_DIR/claude-connector.js.in.new" || die "the downloaded tab file looks wrong"
  mv "$SHARE_DIR/claude-connector.js.in.new" "$SHARE_DIR/claude-connector.js.in"
  printf '%s\n' "$url" >"$SHARE_DIR/connector-url"
  render_js
  if [[ $theme == off ]]; then
    say "Leaving the stock Proxmox look (no theme)"
  else
    say "Installing the brand theme (${theme#pcc-} by default)"
  fi
  # The tab must not depend on the theme: if the theme fails, the tab is installed without it.
  set +e
  (set -e; apply_theme "$theme")
  local rc=$?
  set -e
  if [[ $rc -eq 0 ]]; then
    printf '%s\n' "$theme" >"$SHARE_DIR/theme-choice"
  elif [[ $theme != off && -f "$THEME_JS" && -f "$SHARE_DIR/theme-default" ]]; then
    warn "the theme could not be updated - the installed version stays"
  else
    warn "the theme could not be installed - the tab works without it"
    remove_theme
    theme=off
  fi
  install_self
  cat >"$APT_HOOK" <<EOF
// Puts the Claude Connector tab and theme back after Proxmox web interface upgrades.
// Remove with: pve-claude-gui remove
DPkg::Post-Invoke { "if [ -x $SELF ]; then $SELF reapply >/dev/null 2>&1 || true; fi"; };
EOF
  patch_template || true
  say "Done. Reload the Proxmox page: Datacenter > Claude Connector, and the theme switcher in the top bar."
  if [[ $all == --all-nodes ]]; then
    local args=()
    mapfile -d '' args < <(brand_args)
    if [[ $theme == off ]]; then on_other_nodes install "$url" --no-theme "${args[@]}"
    else on_other_nodes install "$url" --theme "${theme#pcc-}" "${args[@]}"; fi
  fi
  return 0
}

cmd_theme() {
  local mode="" all=""
  while [[ $# -gt 0 ]]; do
    case $1 in
      --all-nodes) all=--all-nodes ;;
      --from)
        [[ $# -ge 2 ]] || die "--from needs a directory"
        use_source "$2"; shift ;;
      -*) die "unknown option: $1" ;;
      *) [[ -z $mode ]] || die "unexpected argument: $1"; mode=$(theme_name "$1") ;;
    esac
    shift
  done
  [[ -n $mode ]] || die "say which theme: dark, light, none or off"
  [[ $EUID -eq 0 ]] || die "run as root on a Proxmox node"
  [[ -f "$SHARE_DIR/connector-url" ]] || die "install the tab first: pve-claude-gui install <connector URL>"
  apply_theme "$mode"
  printf '%s\n' "$mode" >"$SHARE_DIR/theme-choice"
  patch_template || true
  say "Theme: $(theme_summary). Reload the Proxmox page."
  if [[ $all == --all-nodes ]]; then
    if [[ $mode == off ]]; then on_other_nodes theme off; else on_other_nodes theme "${mode#pcc-}"; fi
  fi
  return 0
}

cmd_reapply() {
  [[ -f "$SHARE_DIR/connector-url" && -f "$SHARE_DIR/claude-connector.js.in" ]] || exit 0
  local changed=0 name want
  if [[ ! -f "$SERVED_JS" ]]; then render_js; fi
  if [[ -f "$SHARE_DIR/theme-default" && -f "$SHARE_DIR/theme-files" && -f "$THEME_DIR/SHA256SUMS" ]] \
    && theme_supported; then
    while read -r name; do
      [[ $name =~ $SAFE_NAME ]] || continue
      want=$(theme_file_source "$name")
      if [[ -f $want ]] && ! cmp -s "$want" "$THEMES_DIR/$name"; then
        install -m 0644 "$want" "$THEMES_DIR/$name"
        changed=1
      fi
    done <"$SHARE_DIR/theme-files"
    if [[ $changed -eq 1 || ! -f "$THEME_JS" ]]; then render_theme_js; fi  # the logo version may have changed
  fi
  patch_template || true
  return 0
}

cmd_remove() {
  local all=${1:-}
  [[ $EUID -eq 0 ]] || die "run as root on a Proxmox node"
  [[ $all == --all-nodes ]] && on_other_nodes remove
  say "Removing the Claude Connector tab and theme"
  unpatch_template || true
  remove_theme
  rm -f "$SERVED_JS" "$APT_HOOK"
  rm -rf "$SHARE_DIR"
  say "Removed. Reload the Proxmox page."
  rm -f "$SELF"  # last: safe even when this is the running script
}

cmd_status() {
  local name lost=0 lines
  printf 'tab file:      %s\n' "$([[ -f $SERVED_JS ]] && echo present || echo missing)"
  printf 'theme:         %s\n' "$(theme_summary)"
  if [[ -f "$SHARE_DIR/theme-files" ]]; then
    while read -r name; do [[ -f "$THEMES_DIR/$name" ]] || lost=1; done <"$SHARE_DIR/theme-files"
    printf 'theme files:   %s\n' "$([[ $lost -eq 0 && -f $THEME_JS ]] && echo present || echo missing)"
  fi
  lines=$(grep -c "$MARKER" "$TEMPLATE" 2>/dev/null || true)
  printf 'template:      %s\n' "${lines:-0} line(s) marked $MARKER"
  printf 'upgrade hook:  %s\n' "$([[ -f $APT_HOOK ]] && echo present || echo missing)"
  printf 'connector:     %s\n' "$(cat "$SHARE_DIR/connector-url" 2>/dev/null || echo 'not set')"
  if [[ -f "$SHARE_DIR/brand-name" || -f "$SHARE_DIR/brand-url" || -f "$SHARE_DIR/brand-logo.png" ]]; then
    printf 'brand:         %s%s%s\n' "$(cat "$SHARE_DIR/brand-name" 2>/dev/null || echo 'default name')" \
      "$([[ -f $SHARE_DIR/brand-url ]] && echo ", $(<"$SHARE_DIR/brand-url")")" \
      "$([[ -f $SHARE_DIR/brand-logo.png ]] && echo ', own logo')"
  fi
}

case "${1:-}" in
  install) shift; cmd_install "$@" ;;
  theme)   shift; cmd_theme "$@" ;;
  remove)  shift; cmd_remove "$@" ;;
  reapply) cmd_reapply ;;
  status)  cmd_status ;;
  *) usage; exit 1 ;;
esac
