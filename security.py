"""
Permission layer for the agent.

This module is the one place that decides what the agent is allowed to do.
Keep it separate from tools.py (what the agent CAN do) and main.py (how the
agent thinks) so each piece can be reviewed independently.

Tools are grouped into tiers, checked strictest-concern-first:
  - SAFE_TOOLS       read-only, sandbox containment is the only concern.
  - REVIEW_TOOLS      mutating, gets extra checks beyond containment.
  - RESTRICTED_TOOLS  mutating and hard to undo -- every call must be
                      reviewed by a human before it runs. check_permission
                      returns "needs_approval" for these rather than
                      allowing or denying outright; main.py is responsible
                      for actually pausing and collecting that decision.
"""

import json
import datetime
import pathlib

# --- Tiers -------------------------------------------------------------
SAFE_TOOLS = {"list_files", "read_file"}
REVIEW_TOOLS = {"write_file"}
RESTRICTED_TOOLS = {"delete_file"}

# --- Data directory ----------------------------------------------------
# All runtime-generated state (sandbox contents, logs, checkpoints) lives
# under here, separate from source. This is the one definition of that
# location -- import it, don't redefine it.
DATA_DIR = (pathlib.Path(__file__).parent / "data").resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# --- Sandbox boundary --------------------------------------------------------
# File tools may only touch paths inside this directory. This is the one
# definition of the sandbox location -- import it, don't redefine it.
SANDBOX_DIR = (DATA_DIR / "agent_sandbox").resolve()
SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

AUDIT_LOG_PATH = DATA_DIR / "audit.log"

# --- REVIEW_TOOLS: write_file specifics --------------------------------
WRITE_ALLOWED_EXTENSIONS = {".txt", ".md", ".json"}
WRITE_MAX_CONTENT_BYTES = 100_000  # 100KB


def _check_sandbox_containment(raw_path) -> tuple[bool, str, "pathlib.Path | None"]:
    """Shared containment check: is raw_path inside SANDBOX_DIR? Used by
    every tool that touches the filesystem, read, write, or delete alike,
    so the boundary is defined once."""
    try:
        resolved = pathlib.Path(raw_path).resolve()
    except (OSError, ValueError):
        return False, f"could not resolve path '{raw_path}'", None

    if resolved != SANDBOX_DIR and SANDBOX_DIR not in resolved.parents:
        return False, f"path '{resolved}' is outside sandbox '{SANDBOX_DIR}'", None

    return True, "ok", resolved


def _check_write_file(args: dict) -> tuple[str, str, dict]:
    """Extra checks for the write_file tool, on top of sandbox containment:
    extension allowlist and a content size cap. If the target already
    exists, its current content is returned in `extra` so the caller can
    log it before the overwrite happens."""
    filename = args.get("filename", "")
    content = args.get("content", "")

    contained, reason, resolved = _check_sandbox_containment(SANDBOX_DIR / filename)
    if not contained:
        return "denied", reason, {}

    # Sandbox is flat for now: writes go directly into SANDBOX_DIR, no
    # subfolders. A nested filename (e.g. "sub/x.txt") can still resolve
    # inside the sandbox and pass containment, so this is checked
    # separately rather than relying on write_text() to fail when the
    # parent directory doesn't exist.
    if resolved.parent != SANDBOX_DIR:
        return (
            "denied",
            f"nested paths are not allowed -- sandbox is flat, "
            f"write directly into '{SANDBOX_DIR}' with a bare filename",
            {},
        )

    ext = resolved.suffix.lower()
    if ext not in WRITE_ALLOWED_EXTENSIONS:
        return (
            "denied",
            f"extension '{ext or '(none)'}' is not allowed for writes "
            f"(allowed: {sorted(WRITE_ALLOWED_EXTENSIONS)})",
            {},
        )

    size = len(content.encode("utf-8"))
    if size > WRITE_MAX_CONTENT_BYTES:
        return (
            "denied",
            f"content size {size} bytes exceeds cap of {WRITE_MAX_CONTENT_BYTES} bytes",
            {},
        )

    extra = {}
    if resolved.exists():
        try:
            extra["previous_content"] = resolved.read_text()
        except (OSError, UnicodeDecodeError) as e:
            extra["previous_content"] = f"<could not read previous content: {e}>"

    return "allowed", "ok", extra


def _check_delete_file(args: dict) -> tuple[str, str, dict]:
    """Restricted-tier check for delete_file. Containment and existence are
    verified up front and can deny outright -- there's no point pausing
    for human review on a delete that could never have succeeded. Only a
    call that would actually delete a real, in-sandbox file reaches
    needs_approval, and its current content is read and attached so the
    reviewer can see exactly what's about to be lost."""
    filename = args.get("filename", "")

    contained, reason, resolved = _check_sandbox_containment(SANDBOX_DIR / filename)
    if not contained:
        return "denied", reason, {}

    if not resolved.exists():
        return "denied", f"'{filename}' does not exist -- nothing to delete", {}

    try:
        content = resolved.read_text()
    except (OSError, UnicodeDecodeError) as e:
        content = f"<could not read content: {e}>"

    return "needs_approval", "awaiting human review", {"file_content": content}


def check_permission(tool_name: str, args: dict) -> tuple[str, str, dict]:
    """Return (status, reason, extra).

    status is one of:
      - "allowed"        the call may proceed to tool_execution
      - "denied"         the call is rejected outright
      - "needs_approval" the call requires a human decision before it can
                          proceed; main.py routes these to human_approval
                          instead of tool_execution

    `extra` carries additional context meant for the audit log entry
    (e.g. previous_content on an overwrite, or file_content on a pending
    delete) and is {} when there's nothing extra to record. Called for
    every tool call before execution.
    """
    if tool_name in RESTRICTED_TOOLS:
        if tool_name == "delete_file":
            return _check_delete_file(args)
        return "denied", f"tool '{tool_name}' has no restricted-tier check implemented", {}

    if tool_name in SAFE_TOOLS:
        if tool_name == "read_file":
            raw_path = SANDBOX_DIR / args.get("filename", "")
        else:  # list_files takes no arguments, always the sandbox itself
            raw_path = SANDBOX_DIR
        contained, reason, _ = _check_sandbox_containment(raw_path)
        return ("allowed" if contained else "denied"), reason, {}

    if tool_name in REVIEW_TOOLS:
        if tool_name == "write_file":
            return _check_write_file(args)
        return "denied", f"tool '{tool_name}' has no review-tier check implemented", {}

    return "denied", f"tool '{tool_name}' is not on the allowlist", {}


def audit_log(
    tool_name: str, args: dict, status: str, reason: str, extra: dict | None = None
) -> None:
    """Append one structured line per tool-call attempt or decision. status
    is one of "allowed", "denied", "needs_approval", "approved_by_human",
    "denied_by_human"."""
    entry = {
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "tool": tool_name,
        "args": args,
        "status": status,
        "reason": reason,
    }
    if extra:
        entry.update(extra)
    with AUDIT_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
