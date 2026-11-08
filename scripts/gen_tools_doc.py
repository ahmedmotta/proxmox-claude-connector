"""Regenerate docs/TOOLS.md from the registered tools: python scripts/gen_tools_doc.py"""

import asyncio
import inspect
import os
import tempfile
from pathlib import Path

os.environ.setdefault("PVE_HOST", "pve.local")
os.environ.setdefault("PVE_TOKEN_ID", "root@pam!claude")
os.environ.setdefault("PVE_TOKEN_SECRET", "x")
os.environ.setdefault("CONNECTOR_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("PVE_CONNECTOR_ENV", "/nonexistent")

from pve_connector.config import load_settings  # noqa: E402
from pve_connector.server import build_server  # noqa: E402


async def main() -> None:
    tools = await build_server(load_settings(), remote=False).list_tools()
    lines = [
        "# Tools",
        "",
        "Generated from the code by `scripts/gen_tools_doc.py`. Kind: read = looks only; "
        "change = creates or modifies; danger = can delete, power off, roll back or run commands.",
        "",
        "| Tool | Kind | What it does | Parameters |",
        "|---|---|---|---|",
    ]
    for t in tools:
        a = t.annotations
        kind = "read" if a and a.read_only_hint else "danger" if a and a.destructive_hint else "change"
        desc = inspect.cleandoc(t.description or "").split("\n\n")[0].replace("\n", " ")
        desc = desc.split(". ")[0].rstrip(".") + "."
        props = t.input_schema.get("properties", {})
        required = set(t.input_schema.get("required", []))
        params = ", ".join(f"`{p}`" + ("" if p in required else "?") for p in props) or "none"
        lines.append(f"| `{t.name}` | {kind} | {desc} | {params} |")
    lines += ["", f"{len(tools)} tools. `?` marks optional parameters.", ""]
    Path("docs/TOOLS.md").write_text("\n".join(lines))
    print(f"wrote docs/TOOLS.md ({len(tools)} tools)")


asyncio.run(main())
