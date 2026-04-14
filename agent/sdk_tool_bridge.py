"""Bridge Hermes ToolRegistry to Claude Agent SDK in-process MCP servers.

Iterates the registry, wraps each handler for the SDK's ``@tool`` interface,
and bundles them by toolset into ``create_sdk_mcp_server()`` instances.

Agent-loop tools (memory, todo, session_search) that bypass registry dispatch
are registered as standalone MCP tools with stores injected from a shared
mutable context dict.

Usage::

    from agent.sdk_tool_bridge import build_hermes_mcp_servers

    context = {}  # mutable — caller sets task_id, stores, etc. before each query()
    servers = build_hermes_mcp_servers(context=context)
    # Pass servers to ClaudeAgentOptions(mcp_servers=servers)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

# Tools that overlap with Agent SDK built-ins — skipped by default.
SDK_BUILTIN_OVERLAPS: frozenset[str] = frozenset({
    "read_file", "write_file", "patch", "search_files",
    "terminal",
    "web_search", "web_extract",
})

# Tools handled by the agent loop in run_agent.py — cannot go through
# registry.dispatch() (it returns a stub error).  We register these
# separately with direct function calls + stores from context.
_AGENT_LOOP_TOOLS: frozenset[str] = frozenset({
    "todo", "memory", "session_search",
    # delegate_task → use SDK's native Agent subagent
    # clarify → use SDK's native AskUserQuestion
})

# Tools we skip entirely in SDK mode — replaced by SDK built-ins.
_SDK_NATIVE_REPLACEMENTS: frozenset[str] = frozenset({
    "delegate_task",  # → SDK Agent subagent
    "clarify",        # → SDK AskUserQuestion
})


def _make_handler_wrapper(entry, context: dict):
    """Create an async wrapper that calls a Hermes handler and returns SDK format.

    The wrapper captures *context* by reference so the caller can update it
    (task_id, stores, etc.) before each query() and the tools see fresh values.
    """
    from model_tools import coerce_tool_args

    async def _wrapper(args: Dict[str, Any]) -> Dict[str, Any]:
        # Type-coerce LLM string args (e.g., "42" → 42) using Hermes schemas
        args = coerce_tool_args(entry.name, args)

        try:
            if entry.is_async:
                # Async handler — await directly (we're already in async)
                result = await entry.handler(args, **context)
            else:
                # Sync handler — run in thread to avoid blocking event loop
                result = await asyncio.to_thread(entry.handler, args, **context)
        except Exception as exc:
            logger.exception("Hermes tool %s error: %s", entry.name, exc)
            return {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": f"{type(exc).__name__}: {exc}"}
                )}],
                "is_error": True,
            }

        # Hermes handlers return JSON strings
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False)

        return {"content": [{"type": "text", "text": result}]}

    return _wrapper


def _build_registry_tools(
    context: dict,
    skip_tools: Set[str],
) -> Dict[str, list]:
    """Wrap registry tools grouped by toolset.

    Returns ``{toolset_name: [sdk_tool, ...]}``
    """
    try:
        from claude_agent_sdk import tool as sdk_tool
    except ImportError:
        raise ImportError(
            "claude-agent-sdk is not installed. "
            "Install with: pip install claude-agent-sdk"
        )

    from tools.registry import registry

    grouped: Dict[str, list] = defaultdict(list)

    for name, entry in registry._tools.items():
        # Skip tools handled elsewhere
        if name in skip_tools:
            continue
        if name in _AGENT_LOOP_TOOLS or name in _SDK_NATIVE_REPLACEMENTS:
            continue

        # Run availability check — skip unavailable tools
        if entry.check_fn:
            try:
                if not entry.check_fn():
                    logger.debug("Tool %s unavailable (check failed), skipping", name)
                    continue
            except Exception:
                logger.debug("Tool %s check raised, skipping", name)
                continue

        # Extract schema parts
        description = entry.schema.get("description", entry.description or name)
        parameters = entry.schema.get("parameters", {"type": "object", "properties": {}})

        # Create SDK tool via decorator
        handler_fn = _make_handler_wrapper(entry, context)
        wrapped = sdk_tool(name, description, parameters)(handler_fn)
        grouped[entry.toolset].append(wrapped)

    return dict(grouped)


def _build_agent_loop_tools(context: dict) -> list:
    """Create SDK tools for agent-loop-intercepted tools (memory, todo, session_search).

    These call the underlying functions directly with stores from context.
    """
    try:
        from claude_agent_sdk import tool as sdk_tool
    except ImportError:
        raise ImportError("claude-agent-sdk is not installed.")

    from tools.registry import registry

    tools = []

    # --- memory ---
    memory_entry = registry._tools.get("memory")
    if memory_entry:
        schema = memory_entry.schema
        description = schema.get("description", "Persistent memory across sessions")
        parameters = schema.get("parameters", {"type": "object", "properties": {}})

        @sdk_tool("memory", description, parameters)
        async def memory_handler(args: Dict[str, Any]) -> Dict[str, Any]:
            store = context.get("_memory_store")
            if store is None:
                return {"content": [{"type": "text", "text": json.dumps(
                    {"success": False, "error": "Memory is not available in this session."}
                )}], "is_error": True}

            from tools.memory_tool import memory_tool as _memory_tool
            result = await asyncio.to_thread(
                _memory_tool,
                action=args.get("action"),
                target=args.get("target", "memory"),
                content=args.get("content"),
                old_text=args.get("old_text"),
                store=store,
            )

            # Notify external memory provider of writes
            mm = context.get("_memory_manager")
            if mm and args.get("action") in ("add", "replace"):
                try:
                    mm.on_memory_write(
                        args.get("action", ""),
                        args.get("target", "memory"),
                        args.get("content", ""),
                    )
                except Exception:
                    pass

            return {"content": [{"type": "text", "text": result}]}

        tools.append(memory_handler)

    # --- todo ---
    todo_entry = registry._tools.get("todo")
    if todo_entry:
        schema = todo_entry.schema
        description = schema.get("description", "Task planning and tracking")
        parameters = schema.get("parameters", {"type": "object", "properties": {}})

        @sdk_tool("todo", description, parameters)
        async def todo_handler(args: Dict[str, Any]) -> Dict[str, Any]:
            store = context.get("_todo_store")
            if store is None:
                return {"content": [{"type": "text", "text": json.dumps(
                    {"error": "TodoStore not initialized"}
                )}], "is_error": True}

            from tools.todo_tool import todo_tool as _todo_tool
            result = await asyncio.to_thread(
                _todo_tool,
                todos=args.get("todos"),
                merge=args.get("merge", False),
                store=store,
            )
            return {"content": [{"type": "text", "text": result}]}

        tools.append(todo_handler)

    # --- session_search ---
    ss_entry = registry._tools.get("session_search")
    if ss_entry:
        schema = ss_entry.schema
        description = schema.get("description", "Search past conversation sessions")
        parameters = schema.get("parameters", {"type": "object", "properties": {}})

        @sdk_tool("session_search", description, parameters)
        async def session_search_handler(args: Dict[str, Any]) -> Dict[str, Any]:
            db = context.get("_session_db")
            if db is None:
                return {"content": [{"type": "text", "text": json.dumps(
                    {"success": False, "error": "Session database not available."}
                )}], "is_error": True}

            from tools.session_search_tool import session_search as _session_search
            result = await asyncio.to_thread(
                _session_search,
                query=args.get("query", ""),
                role_filter=args.get("role_filter"),
                limit=args.get("limit", 3),
                db=db,
                current_session_id=context.get("session_id"),
            )
            return {"content": [{"type": "text", "text": result}]}

        tools.append(session_search_handler)

    return tools


def _build_memory_plugin_tools(context: dict) -> list:
    """Bridge tools from the active memory plugin (Honcho, Holographic, etc.).

    Memory providers register extra tools via ``_memory_manager.has_tool()``.
    We bridge them by routing calls to ``_memory_manager.handle_tool_call()``.
    """
    try:
        from claude_agent_sdk import tool as sdk_tool
    except ImportError:
        raise ImportError("claude-agent-sdk is not installed.")

    mm = context.get("_memory_manager")
    if not mm:
        return []

    tools = []
    try:
        schemas = mm.get_tool_schemas() if hasattr(mm, "get_tool_schemas") else []
    except Exception:
        return []

    for schema in schemas:
        tool_name = schema.get("name", "")
        if not tool_name:
            continue
        description = schema.get("description", tool_name)
        parameters = schema.get("parameters", {"type": "object", "properties": {}})

        def _make_mm_handler(tn):
            async def handler(args: Dict[str, Any]) -> Dict[str, Any]:
                mgr = context.get("_memory_manager")
                if not mgr:
                    return {"content": [{"type": "text", "text": json.dumps(
                        {"error": "Memory manager not available"}
                    )}], "is_error": True}
                try:
                    result = await asyncio.to_thread(mgr.handle_tool_call, tn, args)
                except Exception as exc:
                    return {"content": [{"type": "text", "text": json.dumps(
                        {"error": str(exc)}
                    )}], "is_error": True}
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False)
                return {"content": [{"type": "text", "text": result}]}
            return handler

        wrapped = sdk_tool(tool_name, description, parameters)(_make_mm_handler(tool_name))
        tools.append(wrapped)

    return tools


def build_hermes_mcp_servers(
    context: dict,
    skip_tools: Optional[Set[str]] = None,
) -> dict:
    """Build Agent SDK MCP server dict from the Hermes tool registry.

    Args:
        context: Mutable dict shared with all tool handlers. The caller
            updates this before each ``query()`` call with session-specific
            state (task_id, _memory_store, _todo_store, _session_db, etc.).
        skip_tools: Tool names to exclude (defaults to SDK_BUILTIN_OVERLAPS).

    Returns:
        Dict suitable for ``ClaudeAgentOptions(mcp_servers=...)``.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server
    except ImportError:
        raise ImportError(
            "claude-agent-sdk is not installed. "
            "Install with: pip install claude-agent-sdk"
        )

    if skip_tools is None:
        skip_tools = set(SDK_BUILTIN_OVERLAPS)

    # 1. Registry tools grouped by toolset
    grouped = _build_registry_tools(context, skip_tools)

    # 2. Agent-loop tools (memory, todo, session_search)
    agent_loop_tools = _build_agent_loop_tools(context)

    # 3. Memory plugin tools
    mm_tools = _build_memory_plugin_tools(context)

    # Bundle agent-loop + memory-plugin tools into a single "hermes_core" server
    core_tools = agent_loop_tools + mm_tools

    servers = {}

    # Create one MCP server per toolset
    for toolset_name, tool_list in grouped.items():
        server_name = f"hermes_{toolset_name}"
        servers[server_name] = create_sdk_mcp_server(
            name=server_name,
            version="1.0.0",
            tools=tool_list,
        )

    # Core server for agent-loop + plugin tools
    if core_tools:
        servers["hermes_core"] = create_sdk_mcp_server(
            name="hermes_core",
            version="1.0.0",
            tools=core_tools,
        )

    logger.info(
        "Built %d MCP servers with %d total tools for Agent SDK",
        len(servers),
        sum(len(t) if isinstance(t, list) else 0 for t in grouped.values()) + len(core_tools),
    )

    return servers
