# Security policy

This connector can control a hypervisor as root, so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability reporting
(the repository's **Security** tab → **Report a vulnerability**). You will get a reply within a few
days, and a fix and advisory as soon as one is ready.

## Supported versions

Only the latest release receives security fixes.

## Hardening checklist

- Keep `CONNECTOR_BIND_HOST=127.0.0.1` and reach the connector only through your tunnel.
- Use a long admin password and store it hashed (`pve-connector hash-password`).
- Put the connector's own container and any critical guests in `CONNECTOR_PROTECTED_VMIDS`.
- Use `safe` or `readonly` mode if you do not need deletes and shell access.
- Review `/var/lib/pve-connector/audit.log` and `pve-connector sessions` from time to time.
- If something looks wrong, run `pve-connector revoke-all` and delete the API token
  (`pveum user token remove root@pam claude`).
