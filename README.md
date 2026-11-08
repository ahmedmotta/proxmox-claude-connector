# Proxmox Claude Connector

**Run your Proxmox VE servers by talking to Claude — from claude.ai on the web, the desktop app or your phone.**

[![CI](https://github.com/ahmedmotta/proxmox-claude-connector/actions/workflows/ci.yml/badge.svg)](https://github.com/ahmedmotta/proxmox-claude-connector/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A self-hosted [Model Context Protocol](https://modelcontextprotocol.io) server that gives Claude
full, audited control of Proxmox VE. Unlike most Proxmox MCP servers, which run on your own computer,
it is a **remote connector**: it runs next to your Proxmox, is published through a Cloudflare Tunnel, and Claude reaches it from
anywhere after you approve it once with your own password.

> "Create a Debian 12 container with 2 cores and 4 GB, install nginx in it, and snapshot it."
> "Which VMs used the most CPU this week? Shut down the ones tagged `test`."
> "Back up the PBX VM, then upgrade the host packages and tell me if a reboot is needed."

## Features

- **30 tools** covering VMs and containers (create, clone, migrate, configure, resize, power, delete),
  snapshots, backups and restores, storage, ISO and template downloads, tasks, and node health.
- **Commands everywhere**: inside VMs through the QEMU guest agent, inside containers with `pct exec`,
  and on the host itself (`host_shell`) over SSH.
- **Nothing left out**: `proxmox_api` calls any of the Proxmox REST API endpoints — firewall,
  networking, users and permissions, HA, Ceph, replication, apt, certificates.
- **Works with claude.ai web and mobile**: Streamable HTTP transport with a built-in OAuth 2.1
  server (dynamic client registration, PKCE, refresh-token rotation).
- **Guard rails you choose**: `full`, `safe` or `readonly` mode, protected VMIDs that can never be
  stopped or deleted, an optional VMID allowlist, and an audit log of every action.
- **Secure by default**: listens on localhost only, reachable through the tunnel; tokens stored as
  SHA-256 hashes; scrypt-hashed admin password; brute-force lockout; OAuth clients limited to Claude's
  redirect hosts; DNS-rebinding protection.
- **One-command setup over SSH**: creates the API token, a tiny Debian container, the service,
  the Cloudflare tunnel and the SSH trust.
- **Managed from the Proxmox web interface**: a *Datacenter → Claude Connector* tab to pause Claude,
  change permissions, disconnect sessions and read the audit log.

## How it works

```
 Claude (web / desktop / mobile)
        │  HTTPS  (OAuth 2.1 bearer token)
        ▼
 Cloudflare Tunnel  ──►  pve-connector  (small LXC on your Proxmox, listens on 127.0.0.1:8765)
                               │
                ┌──────────────┼──────────────────┐
                ▼              ▼                  ▼
       Proxmox REST API    SSH to nodes     QEMU guest agent
       (API token)         (host shell,     (commands and files
                            pct exec)        inside VMs)
```

## Requirements

- Proxmox VE 8 or 9 (single node or cluster), with root SSH access to one node.
- A domain on Cloudflare (free plan is fine) for the tunnel that lets Claude reach the connector.
- A Claude plan that supports custom connectors.
- About 512 MB of RAM and 4 GB of disk for the connector's container.

## Install over SSH (about 5 minutes)

Everything is installed from one SSH session on a Proxmox node. Nothing is installed on your own
computer, and no port is opened on your router.

### Step 1 — Create the Cloudflare Tunnel

1. In the Cloudflare dashboard open **Zero Trust → Networks → Tunnels** and create a tunnel
   (type *Cloudflared*). Name it e.g. `proxmox-claude`. (Cloudflare renames menus from time to time;
   search the dashboard for "Tunnels" if it has moved.)
2. On the install screen, copy the **token** — the long string after `--token` in any of the
   shown commands. You will paste it in step 3. Do not run Cloudflare's own install command.
3. Add a **public hostname** for the tunnel (called a *published application route* in newer
   dashboards):

   | Field | Value |
   |---|---|
   | Subdomain / domain | e.g. `pve-mcp` . `example.com` |
   | Service type | `HTTP` |
   | URL | `localhost:8765` |

   The connector will then be reachable at `https://pve-mcp.example.com`.

### Step 2 — Connect to a Proxmox node over SSH

From your computer (Linux, macOS, or Windows PowerShell — all have `ssh` built in):

```bash
ssh root@192.168.1.10
```

Replace the address with your Proxmox node's IP. The first time, type `yes` to accept the host key,
then enter the node's root password (the same one you use for the Proxmox web interface with the
`Linux PAM` realm).

> No SSH client at hand? The Proxmox web interface has one: select the node, then **Shell**.

### Step 3 — Run the setup

Paste this into the SSH session:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/ahmedmotta/proxmox-claude-connector/main/deploy/proxmox-setup.sh)"
```

Or do steps 2 and 3 in one line from your computer (`-t` keeps the questions interactive):

```bash
ssh -t root@192.168.1.10 'bash -c "$(curl -fsSL https://raw.githubusercontent.com/ahmedmotta/proxmox-claude-connector/main/deploy/proxmox-setup.sh)"'
```

The script asks:

| Question | Example | Notes |
|---|---|---|
| Public HTTPS URL | `https://pve-mcp.example.com` | The hostname from step 1 |
| Cloudflare Tunnel token | `eyJhIjoi…` | Hidden while typing; Enter to skip and add later |
| Admin password (twice) | — | 12+ characters; typed once when Claude connects |
| Permission mode | `full` | `full`, `safe` or `readonly` — changeable later in the Proxmox GUI |
| Storage | `local-lvm` | Where the connector's container lives |
| Network bridge | `vmbr0` | |
| Container IP | `dhcp` | Or static, e.g. `192.168.1.50/24,gw=192.168.1.1` (recommended) |
| Other protected VMIDs | `100,105` | Guests Claude must never stop or delete (firewall, PBX…) |

Then, without further input, it:

1. creates the API token `root@pam!claude` (full access, privilege separation off);
2. creates an unprivileged Debian container `pve-connector` (1 core, 512 MB RAM, 4 GB disk, starts on boot);
3. installs the connector and `cloudflared` inside it as services;
4. authorizes the connector's SSH key on the node (restricted to the container's IP when it is static);
5. marks the connector's own container as protected, so Claude can never stop or delete it;
6. adds the **Claude Connector** tab to the Proxmox web interface on every cluster node;
7. runs `pve-connector check` and prints a summary with the container's VMID.

A successful run ends with:

```
[ok] Proxmox API https://192.168.1.10:8006/api2/json - version 9.x
[ok] nodes: pve - guests visible: 12
[ok] SSH root@192.168.1.10: pve-manager/9.x
============================================================
 Connector installed in container 120 (protected from Claude).
 ...
```

### Step 4 — Add it to Claude

In Claude open **Settings → Connectors**, add a **custom connector** with the URL

```
https://pve-mcp.example.com/mcp
```

Claude opens the connector's approval page; type your admin password and press **Approve**.
Start a chat and ask: *"Give me an overview of my Proxmox."*

## Manage it from the Proxmox web interface

After installation, reload the Proxmox page (Ctrl+F5) and open **Datacenter → Claude Connector**:

- **Status** — running or paused, the URL to give Claude, mode, SSH, version.
- **Pause connector** — a kill switch: every action from Claude is refused until you resume.
- **Permissions** — switch between `full`, `safe` and `readonly`, edit protected VMIDs and the VMID
  allowlist. Changes apply immediately, without restarting anything.
- **Connected Claude sessions** — see which accounts are connected; disconnect one or all.
- **Recent actions** — the audit log: every tool call, its result and who made it.
- **Change approval password**.

Only Proxmox **administrators** can use the tab. It sends your Proxmox login ticket to the connector,
which asks Proxmox to confirm it and requires `Sys.Modify` on `/`; there are no extra accounts.
Settings changed here are saved in the container (`/var/lib/pve-connector/runtime.json`) and take
precedence over the env file.

The tab survives Proxmox upgrades (an apt hook re-adds it). To manage it on a node, over SSH:

```bash
pve-claude-gui status
pve-claude-gui install https://pve-mcp.example.com --all-nodes   # e.g. after adding a cluster node
pve-claude-gui remove --all-nodes
```

## Update

Over SSH on the Proxmox node (replace `120` with the connector's container VMID):

```bash
pct exec 120 -- bash -c "curl -fsSL https://raw.githubusercontent.com/ahmedmotta/proxmox-claude-connector/main/deploy/install.sh | NONINTERACTIVE=1 bash"
curl -fsSL https://raw.githubusercontent.com/ahmedmotta/proxmox-claude-connector/main/gui/install-gui.sh -o /tmp/install-gui.sh
bash /tmp/install-gui.sh install "$(cat /usr/local/share/pve-claude-connector/connector-url)" --all-nodes
```

Your settings and connected sessions are kept.

## Uninstall

Over SSH on the Proxmox node:

```bash
pct stop 120 && pct destroy 120 --purge            # the connector's container
pveum user token remove root@pam claude            # its API token
pve-claude-gui remove --all-nodes                  # the web interface tab
sed -i --follow-symlinks '/pve-connector@/d' /root/.ssh/authorized_keys   # its SSH key (shared by all nodes)
```

Then delete the tunnel in Cloudflare and remove the connector in Claude's settings.

## Other ways to install

<details>
<summary><b>Installer on an existing Debian/Ubuntu machine, container or VM</b></summary>

```bash
curl -fsSL https://raw.githubusercontent.com/ahmedmotta/proxmox-claude-connector/main/deploy/install.sh | bash
# with a tunnel at the same time:
curl -fsSL https://raw.githubusercontent.com/ahmedmotta/proxmox-claude-connector/main/deploy/install.sh | CF_TUNNEL_TOKEN=... bash
```

It asks for the settings, writes `/etc/pve-connector/env` and starts the `pve-connector` service.
Create the API token yourself first ([below](#create-the-api-token)).
</details>

<details>
<summary><b>Docker Compose (connector + cloudflared)</b></summary>

```bash
git clone https://github.com/ahmedmotta/proxmox-claude-connector.git && cd proxmox-claude-connector
cp .env.example .env        # fill it in
echo "CF_TUNNEL_TOKEN=..." >> .env
docker compose up -d
```

Point the tunnel's public hostname at `http://connector:8765`.
</details>

<details>
<summary><b>Local only: Claude Desktop or Claude Code over stdio</b></summary>

```bash
pip install git+https://github.com/ahmedmotta/proxmox-claude-connector.git
```

```json
{
  "mcpServers": {
    "proxmox": {
      "command": "pve-connector",
      "args": ["stdio"],
      "env": {
        "PVE_HOST": "192.168.1.10",
        "PVE_TOKEN_ID": "root@pam!claude",
        "PVE_TOKEN_SECRET": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "PVE_VERIFY_SSL": "false",
        "CONNECTOR_DATA_DIR": "~/.pve-connector"
      }
    }
  }
}
```

No OAuth is involved in stdio mode; the app on your computer runs the connector directly.
</details>

## Create the API token

The setup script does this for you. By hand, on a Proxmox node:

```bash
# full control (what the connector is designed for)
pveum user token add root@pam claude --privsep 0 --comment "Claude connector"
```

Copy the `value` — it is shown only once. A token of `root@pam` is needed for a few root-only
operations (bind mounts and some container features, hookscripts). For a narrower setup, create a
dedicated user instead and grant only the roles you want:

```bash
pveum user add claude@pve
pveum acl modify / --users claude@pve --roles PVEAdmin
pveum user token add claude@pve connector --privsep 0
```

## Configuration

All settings live in `/etc/pve-connector/env` (see [`.env.example`](.env.example)). After a change:
`systemctl restart pve-connector`.

| Setting | Default | Meaning |
|---|---|---|
| `PVE_HOST` | — | IP or name of any cluster node |
| `PVE_TOKEN_ID` / `PVE_TOKEN_SECRET` | — | API token (`user@realm!name`) and its secret |
| `PVE_VERIFY_SSL` / `PVE_CA_CERT` | `true` / — | TLS check; point `PVE_CA_CERT` at `pve-root-ca.pem` |
| `PVE_SSH_ENABLED` | `false` | Enables `host_shell`, container commands and `api_help` |
| `PVE_SSH_HOST`, `PVE_SSH_USER`, `PVE_SSH_KEY_PATH` | `PVE_HOST`, `root`, — | SSH target and key |
| `CONNECTOR_PUBLIC_URL` | — | The HTTPS address Claude uses (no trailing slash) |
| `CONNECTOR_ADMIN_PASSWORD` | — | Password for the approval page (plain or `pve-connector hash-password`) |
| `CONNECTOR_MODE` | `full` | `full`, `safe` or `readonly` (below) |
| `CONNECTOR_PROTECTED_VMIDS` | — | Guests that can never be stopped, deleted, rolled back or restored over |
| `CONNECTOR_ALLOWED_VMIDS` | — | If set, only these guests are visible and touchable |
| `CONNECTOR_ALLOWED_REDIRECT_HOSTS` | Claude + localhost | Which apps may register through OAuth |
| `CONNECTOR_ACCESS_TOKEN_TTL` / `..._REFRESH_TOKEN_TTL` | 1 h / 30 days | Token lifetimes |
| `CONNECTOR_MAX_WAIT` | 90 s | Longest a tool waits for a Proxmox task before handing back its id for `get_task` |

### Permission modes

| Mode | Read | Create / change / start | Delete, stop, reset, rollback, restore over | Shell & guest commands |
|---|:-:|:-:|:-:|:-:|
| `full` | ✓ | ✓ | ✓ | ✓ |
| `safe` | ✓ | ✓ | — | — |
| `readonly` | ✓ | — | — | — |

- `safe` allows clean shutdowns and reboots, but not hard stops. Through the raw `proxmox_api` tool it
  allows only a short list of harmless changes (start, snapshot, backup, downloads, apt index refresh);
  every other change counts as destructive. Reading files inside guests or backups counts as shell access.
- **Protected VMIDs** still allow reading, starting, snapshots, backups and commands inside the guest —
  everything except shutting it down, stopping it or destroying its data.
- **The VMID allowlist** hides other guests everywhere and turns off `proxmox_api` and `host_shell`,
  because raw API and root shell access cannot be limited to some guests.

## Tools

See [docs/TOOLS.md](docs/TOOLS.md) for the full table. In short:

| Area | Tools |
|---|---|
| Overview | `get_overview`, `node_status`, `list_guests`, `get_guest` |
| Lifecycle | `create_vm`, `create_container`, `clone_guest`, `migrate_guest`, `power_guest`, `update_guest_config`, `resize_disk`, `convert_to_template`, `delete_guest` |
| Protection | `manage_snapshots`, `backup_guests`, `list_backups`, `restore_backup` |
| Storage | `list_storage`, `storage_content`, `download_to_storage`, `list_lxc_templates`, `download_lxc_template` |
| Tasks | `list_tasks`, `get_task` |
| Commands | `guest_exec`, `guest_file`, `host_shell` |
| Anything else | `proxmox_api`, `api_help` |
| The connector | `connector_info` (mode, protected guests, recent audit log) |

## Day-to-day commands

Inside the connector's container (`pct enter <id>`):

```bash
pve-connector check          # test the API token, SSH and settings
pve-connector sessions       # list connected Claude sessions
pve-connector revoke-all     # disconnect every session immediately
pve-connector hash-password  # make a hash for CONNECTOR_ADMIN_PASSWORD
journalctl -u pve-connector -f
tail -f /var/lib/pve-connector/audit.log
```

## Security

Giving an AI root on a hypervisor is a real decision. The connector is built so that the decision is
yours and every action is visible:

- Only someone with the admin password can connect a Claude account, and only Claude's own redirect
  addresses can register. Sessions can be listed and revoked at any time.
- The service listens on `127.0.0.1`; the only way in is your tunnel.
- Each tool is marked read-only or destructive in its MCP annotations, and the server instructions tell
  Claude to prefer clean shutdowns and to snapshot or back up before risky changes.
- `safe` and `readonly` modes and protected VMIDs are enforced on the server, not by the model.
- The Proxmox GUI tab has no password of its own: it forwards the logged-in user's Proxmox ticket in a
  header (never a cookie), the connector asks Proxmox to validate it, and only users with `Sys.Modify`
  on `/` are accepted. Tickets are never stored or logged.
- Every call — tool, arguments, result, client — is appended to `audit.log` (readable by its owner only).
  Passwords, tokens, keys, file contents and stdin are redacted; shell commands are kept on purpose,
  so you can see exactly what was run.
- `proxmox_api` refuses paths with `..`, encoding, queries or doubled slashes, and checks every VMID in
  the path and in the parameters, so the policy always judges exactly what is sent.
- The setup script adds the connector's SSH key to `/root/.ssh/authorized_keys`. On Proxmox that file is
  shared by every node in the cluster, so the connector gets root SSH on all nodes. With a static
  container IP the key is limited to that address. Use `PVE_SSH_ENABLED=false` if you do not want this.

Found a vulnerability? See [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Proxmox API error 401` | Wrong token id/secret, or the token was deleted |
| `Proxmox API error 403 ... Permission check failed` | Token has privilege separation on, or lacks a role: recreate with `--privsep 0` |
| `CERTIFICATE_VERIFY_FAILED` | Set `PVE_CA_CERT` to a copy of `/etc/pve/pve-root-ca.pem`, or `PVE_VERIFY_SSL=false` on a trusted LAN |
| Claude says it cannot connect | Check `https://<your-host>/health` in a browser, then the tunnel's public hostname target |
| `QEMU guest agent is not running` | Install `qemu-guest-agent` in the VM and set *QEMU Guest Agent* on in its Options |
| Container/host commands say SSH is off | Set `PVE_SSH_ENABLED=true` and authorize the key from `/var/lib/pve-connector/id_ed25519.pub` |
| Service fails with namespace errors in an LXC | Enable `nesting=1` on the connector's container |
| No *Claude Connector* tab in Datacenter | Reload with Ctrl+F5; on that node run `pve-claude-gui status`, then `pve-claude-gui install <url>` |
| The tab says it cannot reach the connector | Open `https://<your-host>/health` from the same browser; the browser needs internet access to reach the tunnel |
| The tab says you are not an administrator | Log in to Proxmox as a user with `Sys.Modify` on `/` (e.g. `root@pam` or the Administrator role) |

## Development

```bash
git clone https://github.com/ahmedmotta/proxmox-claude-connector.git && cd proxmox-claude-connector
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest            # mocked Proxmox API + a full OAuth round trip, no server needed
ruff check src tests
python scripts/gen_tools_doc.py   # refresh docs/TOOLS.md after changing tools
```

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © Innovation IT Hub — [innithub.online](https://innithub.online)

Proxmox® is a registered trademark of Proxmox Server Solutions GmbH. Claude is a product of Anthropic.
This project is independent and not affiliated with either.
