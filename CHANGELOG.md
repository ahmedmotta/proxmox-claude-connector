# Changelog

## GUI 0.4.0 — 2026-11-08

A complete redesign of the Proxmox web interface theme, from a recolor into a console layout:

- Full-bleed app shell: a taller top bar with the brand, a version badge and a wide search, a
  sidebar for the resource tree, a page header and a section navigation with rounded selections.
- Every titled panel becomes a card with rounded corners on a darker canvas, and Proxmox's own
  spacing becomes the gutter between cards.
- Modern tables (uppercase headers, taller rows, no column rules), gradient primary buttons,
  rounded inputs with a soft focus ring, elevated menus and windows, a blurred dialog backdrop.
- A full-screen, branded sign-in page.
- Charts with a fill that fades from the line to the axis.
- Light theme rebuilt to match: white surfaces on a light canvas, white top bar.

## GUI 0.3.0 — 2026-11-08

A new look for the whole Proxmox VE web interface, installed with the tab:

- Dark and light themes built on Proxmox's own theme mechanism (`/pwt/themes/`), listed in
  *Color Theme* and switchable from a new dark / light / stock switcher in the top bar.
- Black brand bar with logo and name, Inter for text and Space Grotesk for titles (bundled, sized to
  keep every ExtJS layout intact), one orange accent, neutral selection with an orange rail, brand
  chart and gauge colors, and one orange button per dialog (Back, Reset, No, Cancel are neutral).
- Text contrast meets WCAG AA in both themes.
- First visit switches a browser to the default theme (`--theme dark|light|none`); `?pcc-theme=off`
  always returns a browser to the stock look.
- `pve-claude-gui theme dark|light|none|off [--all-nodes]`, `--no-theme`, and `--brand`,
  `--brand-url`, `--brand-logo` for your own name, link and logo. The choice is kept across updates.
- The installer checks every theme file against `SHA256SUMS` before copying anything and only uses
  files it downloaded itself, a git checkout or the copy another cluster node sent along. It no
  longer restarts pveproxy (not needed; open consoles and shells keep running), writes the GUI
  template atomically, adds a content hash to the script URLs so browsers never keep a stale copy,
  and keeps the tab working if the theme cannot be installed. Works on Proxmox VE 7.4, 8 and 9.
- A browser still set to a brand theme after the theme was turned off goes back to Proxmox's default.

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
