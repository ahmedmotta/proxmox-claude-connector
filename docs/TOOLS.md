# Tools

Generated from the code by `scripts/gen_tools_doc.py`. Kind: read = looks only; change = creates or modifies; danger = can delete, power off, roll back or run commands.

| Tool | Kind | What it does | Parameters |
|---|---|---|---|
| `get_overview` | read | Summary of the whole Proxmox environment: version, cluster and quorum, every node's CPU, memory and uptime, guest counts, storage usage, and the connector's own permission mode. | none |
| `list_guests` | read | List virtual machines (qemu) and containers (lxc) across the cluster with their VMID, name, node, status, CPU, memory, disk, uptime, tags and whether they are templates. | `node`?, `type`?, `status`?, `search`? |
| `get_guest` | read | Full details of one VM or container: live status, configuration (disks, network, CPU, memory, boot order, cloud-init), snapshots count, and IP addresses reported by the guest agent (VMs) or the container interfaces (LXC). | `vmid`, `include_config`? |
| `node_status` | read | Hardware and health of a node: CPU model and load, memory, swap, root disk, kernel, Proxmox version, boot mode, plus its network interfaces and physical disks. | `node`? |
| `power_guest` | danger | Start, cleanly shut down, reboot, hard-stop, reset, suspend or resume a VM or container. | `vmid`, `action`, `wait`?, `timeout_seconds`? |
| `update_guest_config` | change | Change configuration keys of a VM or container, exactly as in its config file. | `vmid`, `settings`?, `delete`? |
| `resize_disk` | change | Grow a VM disk (e.g. | `vmid`, `disk`, `size` |
| `create_vm` | change | Create a QEMU virtual machine. | `settings`, `node`?, `vmid`?, `start`?, `wait`? |
| `create_container` | change | Create an LXC container. | `settings`, `node`?, `vmid`?, `start`?, `wait`? |
| `clone_guest` | change | Clone a VM, container or template into a new guest. | `vmid`, `name`?, `newid`?, `full`?, `target_node`?, `storage`?, `wait`? |
| `migrate_guest` | change | Move a VM or container to another cluster node. | `vmid`, `target_node`, `online`?, `wait`? |
| `convert_to_template` | danger | Turn a stopped VM or container into a template for fast cloning. | `vmid` |
| `delete_guest` | danger | Permanently delete a VM or container and its disks. | `vmid`, `purge`?, `wait`? |
| `manage_snapshots` | danger | List, create, roll back to, or delete snapshots of a VM or container. | `vmid`, `action`, `name`?, `description`?, `include_ram`?, `wait`? |
| `backup_guests` | change | Run an immediate backup (vzdump) of one or more guests to a backup storage (local, NFS, Proxmox Backup Server...). | `vmids`, `storage`?, `mode`?, `compress`?, `notes`?, `protected`?, `wait`?, `timeout_seconds`? |
| `list_backups` | read | List backup files (volid, guest, date, size, notes, protected). | `storage`?, `vmid`?, `node`? |
| `restore_backup` | danger | Restore a backup (`archive` is the volid from list_backups) as a new guest, or over an existing one with `overwrite=true` (destroys the current guest's data). | `archive`, `vmid`?, `node`?, `storage`?, `overwrite`?, `start`?, `unique`?, `wait`? |
| `list_storage` | read | Storage on a node with type, allowed content (images, iso, vztmpl, backup...), usage and whether it is shared. | `node`? |
| `storage_content` | read | Files and volumes on one storage: disk images, ISO files, container templates, backups, snippets or import images. | `storage`, `node`?, `content`? |
| `download_to_storage` | change | Have Proxmox download a file straight from the internet into a storage: an installer ISO (content="iso"), a container template (content="vztmpl") or a cloud/disk image for importing (content="import", e.g. | `url`, `storage`, `filename`, `content`?, `node`?, `checksum`?, `checksum_algorithm`?, `wait`? |
| `list_lxc_templates` | read | The official container template catalog (Debian, Ubuntu, Alpine, Rocky, TurnKey apps...) that can be downloaded onto a node with download_lxc_template. | `node`?, `search`? |
| `download_lxc_template` | change | Download a template from the official catalog (name from list_lxc_templates) into a storage that holds container templates. | `template`, `storage`?, `node`? |
| `list_tasks` | read | Recent Proxmox tasks (backups, starts, migrations, clones...) with result, user and duration, newest first. | `node`?, `vmid`?, `running_only`?, `errors_only`?, `limit`? |
| `get_task` | read | Status and log of a Proxmox task by its UPID. | `upid`, `wait`?, `timeout_seconds`? |
| `guest_exec` | danger | Run a shell command inside a guest and return its exit code and output. | `vmid`, `command`, `timeout_seconds`?, `stdin`? |
| `guest_file` | danger | Read a text file from, or write a text file into, a running VM (guest agent) or container (SSH). | `vmid`, `action`, `path`, `content`? |
| `host_shell` | danger | Run a shell command as root on a Proxmox node over SSH: pvesh, qm, pct, pvesm, zfs, apt, journalctl, systemctl, ip, smartctl and so on. | `command`, `node`?, `timeout_seconds`? |
| `proxmox_api` | danger | Call any Proxmox VE REST API endpoint, e.g. | `method`, `path`, `params`?, `wait_for_task`? |
| `api_help` | read | Show the methods and parameters of a Proxmox API path (from `pvesh usage`), e.g. | `path` |
| `connector_info` | read | The connector's own version, permission mode, protected VMIDs, whether SSH is enabled, and the latest entries of its audit log (every action taken through it). | `audit_entries`? |

30 tools. `?` marks optional parameters.
