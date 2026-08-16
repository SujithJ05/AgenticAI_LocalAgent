"""
Entry point. Wires together: agent (thinks) -> permission_gate (decides)
-> tool_execution (acts) -> back to agent -> end. Restricted-tier calls
detour through human_approval before they can reach tool_execution.

The agent node never executes anything directly. Every tool call it
requests passes through permission_gate first.
"""

import sqlite3
import uuid
from typing import Annotated, Sequence, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_ollama import ChatOllama
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

import pending_runs
from security import DATA_DIR, audit_log, check_permission
from tools import TOOL_MAP, TOOLS

MODEL_NAME = "gemma4:e4b"

# Checkpoints are stored on disk, not in memory, so a paused run (e.g. one
# sitting at human_approval) survives the process that started it dying --
# a fresh process pointed at the same thread_id can pick it back up.
CHECKPOINT_DB_PATH = DATA_DIR / "checkpoints.db"


class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    approved_calls: list
    pending_approvals: list


llm = ChatOllama(model=MODEL_NAME, temperature=0).bind_tools(TOOLS)


def agent_node(state: AgentState) -> dict:
    """The only node allowed to call the model. Produces a response or a
    request for tool calls -- never executes anything itself."""
    response = llm.invoke(state["messages"])
    return {"messages": [response]}


def permission_gate(state: AgentState) -> dict:
    """Checks every requested tool call against the allowlist before
    anything is allowed to run. Denied calls get a rejection message
    instead of execution. Calls that need a human decision are routed to
    human_approval rather than being allowed or denied here. Every
    attempt is logged, whatever the outcome."""
    last = state["messages"][-1]
    requested = getattr(last, "tool_calls", None) or []

    approved, rejections, pending = [], [], []
    for call in requested:
        status, reason, extra = check_permission(call["name"], call["args"])
        audit_log(call["name"], call["args"], status, reason, extra)
        if status == "allowed":
            approved.append(call)
        elif status == "needs_approval":
            pending.append({"call": call, "extra": extra})
        else:
            rejections.append(
                ToolMessage(content=f"blocked: {reason}", tool_call_id=call["id"])
            )

    return {
        "messages": rejections,
        "approved_calls": approved,
        "pending_approvals": pending,
    }


def human_approval(state: AgentState, config: RunnableConfig) -> dict:
    """Pauses for a human decision on every call permission_gate flagged
    needs_approval. Prints the tool, target filename, and the file's
    current content, records the pending run in pending_runs.log so it
    can be found without already knowing its thread_id, then interrupts
    and waits for the driving loop to resume with a y/n answer. An
    approval proceeds to tool_execution exactly like a normal approved
    call; a denial is logged and returned to the agent as a rejection,
    exactly like a permission_gate denial."""
    thread_id = config["configurable"]["thread_id"]
    approved, rejections = [], []
    for item in state.get("pending_approvals", []):
        call, extra = item["call"], item["extra"]
        filename = call["args"].get("filename", "?")
        content = extra.get("file_content", "(no content)")

        print("\n" + "=" * 60)
        print("HUMAN APPROVAL REQUIRED")
        print(f"  tool:     {call['name']}")
        print(f"  filename: {filename}")
        print("  current content:")
        print("  " + "-" * 40)
        for line in (content.splitlines() or [""]):
            print(f"  {line}")
        print("  " + "-" * 40)
        print("=" * 60)

        # Written before interrupt() pauses, not after: if the process
        # dies while paused, this is the only record the run exists at
        # all -- same reasoning as the checkpoint-before-crash property.
        pending_runs.mark_pending(thread_id, call["name"], call["args"])

        answer = interrupt(
            {
                "tool": call["name"],
                "filename": filename,
                "content": content,
                "prompt": f"Approve {call['name']}('{filename}')? (y/n): ",
            }
        )

        if str(answer).strip().lower() in ("y", "yes"):
            pending_runs.mark_resolved(thread_id, "approved")
            audit_log(
                call["name"], call["args"], "approved_by_human",
                "approved by human review", extra,
            )
            approved.append(call)
        else:
            pending_runs.mark_resolved(thread_id, "denied")
            audit_log(
                call["name"], call["args"], "denied_by_human",
                "denied by human review", extra,
            )
            rejections.append(
                ToolMessage(
                    content="blocked: denied by human review",
                    tool_call_id=call["id"],
                )
            )

    return {"messages": rejections, "approved_calls": approved, "pending_approvals": []}


def tool_execution(state: AgentState) -> dict:
    """Executes only calls that already passed permission_gate or human_approval."""
    outputs = []
    for call in state.get("approved_calls", []):
        tool_fn = TOOL_MAP[call["name"]]
        try:
            result = tool_fn.invoke(call["args"])
        except Exception as e:  # noqa: BLE001 - surface tool errors to the model
            result = f"error: {e}"
        outputs.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
    return {"messages": outputs, "approved_calls": []}


def route_after_agent(state: AgentState) -> str:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "permission_gate"
    return END


def route_after_permission(state: AgentState) -> str:
    if state.get("pending_approvals"):
        return "human_approval"
    if state.get("approved_calls"):
        return "tool_execution"
    return "agent"  # everything was rejected -- let the model respond to that


def route_after_human_approval(state: AgentState) -> str:
    if state.get("approved_calls"):
        return "tool_execution"
    return "agent"  # everything was denied -- let the model respond to that


graph = StateGraph(AgentState)
graph.add_node("agent", agent_node)
graph.add_node("permission_gate", permission_gate)
graph.add_node("human_approval", human_approval)
graph.add_node("tool_execution", tool_execution)

graph.set_entry_point("agent")
graph.add_conditional_edges(
    "agent", route_after_agent, {"permission_gate": "permission_gate", END: END}
)
graph.add_conditional_edges(
    "permission_gate",
    route_after_permission,
    {
        "human_approval": "human_approval",
        "tool_execution": "tool_execution",
        "agent": "agent",
    },
)
graph.add_conditional_edges(
    "human_approval",
    route_after_human_approval,
    {"tool_execution": "tool_execution", "agent": "agent"},
)
graph.add_edge("tool_execution", "agent")

# check_same_thread=False: our scripts only ever use this connection from
# the thread that created it, but the sqlite3 default would otherwise
# reject reuse across the sync call boundaries LangGraph's checkpointer
# goes through internally.
_conn = sqlite3.connect(str(CHECKPOINT_DB_PATH), check_same_thread=False)
checkpointer = SqliteSaver(_conn)
checkpointer.setup()

app = graph.compile(checkpointer=checkpointer)


def run(initial_state: dict, thread_id: str | None = None) -> dict:
    """Start a new run and drive it to completion, transparently resuming
    past any human_approval interrupts by reading a y/n answer from the
    terminal. Because checkpoints are persisted to CHECKPOINT_DB_PATH,
    if this process dies while paused at an interrupt, the same
    thread_id printed below can be handed to resume() in a fresh
    process to pick the run back up -- nothing is lost."""
    thread_id = thread_id or str(uuid.uuid4())
    print(f"[thread_id={thread_id}]")
    config = {"configurable": {"thread_id": thread_id}}
    result = app.invoke(initial_state, config=config)
    return _drive_to_completion(result, config)


def resume(thread_id: str, answer: str) -> dict:
    """Resume a run that's paused at human_approval, in a fresh process
    that never saw the original initial_state -- the checkpointer
    restores everything from CHECKPOINT_DB_PATH using just the
    thread_id. `answer` is the y/n response to the pending prompt."""
    config = {"configurable": {"thread_id": thread_id}}
    result = app.invoke(Command(resume=answer), config=config)
    return _drive_to_completion(result, config)


def _drive_to_completion(result: dict, config: dict) -> dict:
    """Shared tail end of run()/resume(): keep resuming interrupts with
    terminal y/n input until the graph reaches a real end state."""
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        answer = input(payload.get("prompt", "Approve? (y/n): "))
        result = app.invoke(Command(resume=answer), config=config)
    return result


if __name__ == "__main__":
    result = run(
        {
            "messages": [
                HumanMessage(
                    content="List the files in the sandbox directory, "
                    "then also try reading C:\\Windows\\System32\\drivers\\etc\\hosts."
                )
            ],
            "approved_calls": [],
            "pending_approvals": [],
        }
    )
    for m in result["messages"]:
        print(f"[{m.type}] {m.content}")
