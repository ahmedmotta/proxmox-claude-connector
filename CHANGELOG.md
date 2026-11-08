# Changelog

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
