"""
Tool definitions. This module only defines CAPABILITY, not PERMISSION.
Whether a given tool call is actually allowed to run is decided in
security.py, never here.
"""

from langchain_core.tools import tool

from security import SANDBOX_DIR


@tool
def list_files() -> str:
    """List files in the sandbox directory. Takes no arguments -- always
    lists the sandbox itself; there is no way to point it anywhere else."""
    return "\n".join(str(f) for f in SANDBOX_DIR.iterdir()) or "(empty directory)"


@tool
def read_file(filename: str) -> str:
    """Read a text file's contents from the sandbox. Takes a bare filename,
    not a path -- the full path is always constructed by joining it with
    the sandbox directory, so there is no way to point a read outside
    the sandbox."""
    p = SANDBOX_DIR / filename
    if not p.exists():
        return f"error: '{filename}' does not exist"
    return p.read_text()


@tool
def write_file(filename: str, content: str) -> str:
    """Write text content to a file in the sandbox. Takes a bare filename,
    not a path -- the full path is always constructed by joining it with
    the sandbox directory, so there is no way to point a write outside
    the sandbox. Allowed extensions and size limits are enforced by the
    permission layer before this runs."""
    p = SANDBOX_DIR / filename
    p.write_text(content)
    return f"wrote {len(content.encode('utf-8'))} bytes to {p}"


@tool
def delete_file(filename: str) -> str:
    """Delete a file from the sandbox. Takes a bare filename, not a path --
    the full path is always constructed by joining it with the sandbox
    directory. This is a restricted-tier action: the permission layer
    requires human approval before a call ever reaches this function."""
    p = SANDBOX_DIR / filename
    if not p.exists():
        return f"error: '{filename}' does not exist"
    p.unlink()
    return f"deleted {p}"


TOOLS = [list_files, read_file, write_file, delete_file]
TOOL_MAP = {t.name: t for t in TOOLS}