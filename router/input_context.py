import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InputContext:
    task: str
    project_root: str | None
    has_tool_history: bool
    has_opaque_history: bool
    tasks: tuple[str, ...] = ()

    def task_key(self, session_id: str | None, task: str | None = None) -> str:
        value = json.dumps(
            [session_id, self.project_root, self.task if task is None else task], ensure_ascii=False
        )
        return "task:" + hashlib.sha256(value.encode()).hexdigest()


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
        return InputContext(items, root, False, False)
    task = ""
    tools = False
    opaque = False
    tasks = []
    for item in items:
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
    return InputContext(task, root, tools, opaque, tuple(tasks))
