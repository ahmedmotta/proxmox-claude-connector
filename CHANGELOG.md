# Changelog

## GUI tab 0.2.0 — 2026-11-08

Redesigned the Datacenter → Claude Connector tab:

- Status strip that says in one sentence whether Claude can act, with a one-click pause.
- Access level as three plain-language choices instead of a drop-down.
- Guests shown as a rack with a switch to protect each one, and an optional "only the guests
  I tick" scope, instead of typing VMIDs.
- Activity written as sentences ("Shut down guest 105"), filterable to changes or failures,
  with details on click; refreshes every 30 seconds.
- Clear guidance when the connector cannot be reached; follows Proxmox's dark theme.

## 0.1.0 — 2026-11-08

First release.

- Remote MCP connector (Streamable HTTP) with a built-in OAuth 2.1 server: dynamic client registration,
  PKCE, password approval page, refresh-token rotation, hashed token storage, brute-force lockout.
- 30 tools: overview, guests, lifecycle, configuration, snapshots, backups and restores, storage,
  downloads, tasks, guest commands and files, host shell, and a generic Proxmox API tool.
- Permission modes (`full`, `safe`, `readonly`), protected VMIDs, VMID allowlist and an audit log.
- **Datacenter → Claude Connector** tab in the Proxmox web interface: status, pause switch, live
  permission changes, connected sessions, audit log and approval password. Authenticated by the Proxmox
  login (administrators only), installed on every cluster node and kept across Proxmox upgrades.
- One-command setup over SSH on a Proxmox node, Debian installer, Docker Compose with cloudflared, stdio mode.
