# Contributing

Thanks for helping! Issues and pull requests are welcome.

1. Fork and create a branch.
2. `pip install -e ".[dev]"`
3. Make the change, with a test in `tests/` (the Proxmox API is mocked with `respx`, so no server is needed).
4. `ruff format src tests && ruff check src tests && pytest`
5. If you added or changed a tool, run `python scripts/gen_tools_doc.py` to refresh `docs/TOOLS.md`.
6. Open a pull request describing what it does and how you tested it on a real Proxmox, if you did.

## Adding a tool

Tools live in `src/pve_connector/tools.py`. Each one:

- is declared with `@tool(Kind.READ | Kind.WRITE | Kind.DESTRUCTIVE | Kind.SHELL, "Title")`, which sets
  its MCP annotations and wraps it with error handling and the audit log;
- calls `policy.check(kind, vmid)` before doing anything, so modes, protected VMIDs and the
  allowlist apply;
- has a docstring written for Claude: what it does, with a concrete example of its parameters.

## The Proxmox GUI tab

`gui/claude-connector.js` is plain ExtJS loaded after `pvemanagerlib.js`. Keep it free of
`'use strict'` (ExtJS `callParent` needs `Function.caller`), wrap anything that touches Proxmox
classes so a failure can never break the Proxmox interface, and test on Proxmox VE 8 and 9.
