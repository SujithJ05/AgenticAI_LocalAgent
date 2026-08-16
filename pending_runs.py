"""
Pending-approval tracker.

A small JSON-lines log so a paused run can be found without already
knowing its thread_id -- mirrors audit.log's append-only,
one-JSON-object-per-line pattern, but indexes by thread_id/run rather
than by tool call.
"""

import json
import datetime

from security import DATA_DIR

PENDING_RUNS_LOG_PATH = DATA_DIR / "pending_runs.log"


def mark_pending(thread_id: str, tool: str, args: dict) -> None:
    """Record that a run has paused awaiting human approval. Must be
    called before interrupt() fires, not after -- if the process dies
    while paused, this entry is the only way to ever find the run
    again, the same reasoning as writing the checkpoint before the
    pause rather than after."""
    _append(
        {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "thread_id": thread_id,
            "status": "pending",
            "tool": tool,
            "args": args,
        }
    )


def mark_resolved(thread_id: str, outcome: str) -> None:
    """Record that a previously-pending run has been decided. `outcome`
    is typically "approved" or "denied"."""
    _append(
        {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "thread_id": thread_id,
            "status": "resolved",
            "outcome": outcome,
        }
    )


def list_pending() -> list[dict]:
    """Return the most recent entry for every thread_id whose latest
    entry is still "pending" -- i.e. no later "resolved" entry exists
    for that thread_id."""
    if not PENDING_RUNS_LOG_PATH.exists():
        return []

    latest_by_thread = {}
    with PENDING_RUNS_LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            latest_by_thread[entry["thread_id"]] = entry  # later lines win

    return [e for e in latest_by_thread.values() if e["status"] == "pending"]


def _append(entry: dict) -> None:
    with PENDING_RUNS_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
