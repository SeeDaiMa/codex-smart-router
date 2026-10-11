import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


def history_keys(items: Any) -> tuple[str, ...]:
    """Link opaque/tool items across resumed and compacted transcripts using hashes only."""
    keys = []
    if not isinstance(items, list):
        return ()
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        identity = None
        if kind in {"reasoning", "compaction"} and item.get("encrypted_content"):
            identity = [kind, item["encrypted_content"]]
        elif kind in {
            "function_call",
            "function_call_output",
            "custom_tool_call",
            "custom_tool_call_output",
        } and item.get("call_id"):
            identity = ["tool", item["call_id"]]
        if identity is not None:
            digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            keys.append("history:" + digest)
    return tuple(dict.fromkeys(keys))


@dataclass(frozen=True)
class InputContext:
    task: str
    project_root: str | None
    has_tool_history: bool
    has_opaque_history: bool
    tasks: tuple[str, ...] = ()
    new_user_turn: bool = False
    active_history: tuple[str, ...] = ()
    turn_prefix: tuple[str, ...] = ()

    def task_key(self, session_id: str | None, task: str | None = None) -> str:
        value = json.dumps(
            [session_id, self.project_root, self.task if task is None else task], ensure_ascii=False
        )
        return "task:" + hashlib.sha256(value.encode()).hexdigest()

    def turn_key(self, session_id: str | None) -> str:
        value = json.dumps(
            [session_id, self.project_root, self.turn_prefix, self.task], ensure_ascii=False
        )
        return "turn:" + hashlib.sha256(value.encode()).hexdigest()


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") in {"input_text", "text"}
        and isinstance(item.get("text"), str)
    )


def extract_context(body: dict[str, Any]) -> InputContext:
    metadata = body.get("metadata")
    root = metadata.get("smart_router_project_root") if isinstance(metadata, dict) else None
    root = root if isinstance(root, str) else None
    explicit_root = root is not None
    items = body.get("input", [])
    if isinstance(items, str):
        return InputContext(items, root, False, False, (items,), bool(items.strip()))
    task = ""
    tools = False
    opaque = False
    tasks = []
    latest_user = -1
    last_progress = -1
    pending_calls = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        kind = item.get("type", "message")
        tools |= kind in {
            "function_call",
            "function_call_output",
            "custom_tool_call",
            "custom_tool_call_output",
        }
        opaque |= kind == "compaction" or (
            kind == "reasoning" and bool(item.get("encrypted_content"))
        )
        call_id = item.get("call_id")
        if isinstance(call_id, str):
            if kind in {"function_call", "custom_tool_call"}:
                pending_calls.add(call_id)
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                pending_calls.discard(call_id)
        if (
            kind
            in {
                "function_call",
                "function_call_output",
                "custom_tool_call",
                "custom_tool_call_output",
                "reasoning",
                "compaction",
            }
            or item.get("role") == "assistant"
        ):
            last_progress = index
        if item.get("role") != "user":
            continue
        text = message_text(item.get("content"))
        # Context is data; project paths only gain access through the server's allowlist.
        matches = re.findall(r"<cwd>\s*([^<>]+?)\s*</cwd>", text[:131072])
        if matches and not explicit_root:
            root = matches[-1]
        stripped = text.strip()
        if stripped.startswith(
            ("# AGENTS.md instructions", "<environment_context>", "<external_codex_apps_open_page>")
        ):
            continue
        if stripped:
            task = stripped
            tasks.append(task)
            latest_user = index
    prefix = (
        tuple("task:" + hashlib.sha256(text.encode()).hexdigest() for text in tasks[:-1])
        + history_keys(items[:latest_user])
        if latest_user >= 0
        else ()
    )
    return InputContext(
        task,
        root,
        tools,
        opaque,
        tuple(tasks),
        latest_user > last_progress and not pending_calls,
        history_keys(items[latest_user + 1 :]),
        prefix,
    )
