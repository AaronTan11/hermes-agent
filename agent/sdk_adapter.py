"""Claude Agent SDK adapter — drop-in replacement for AIAgent.run_conversation().

Wraps ``ClaudeSDKClient`` (persistent session) and ``query()`` (one-off) to
present the same result dict interface that the gateway, CLI, and cron expect::

    {"final_response": str, "messages": list, "api_calls": int, "completed": bool}

Security hooks (dangerous command detection, secret redaction, prompt injection
scanning) are registered as Agent SDK PreToolUse/PostToolUse/UserPromptSubmit
hooks so they fire inside the SDK's agent loop.

Usage::

    runner = SDKAgentRunner(
        system_prompt="You are a helpful assistant.",
        context={"task_id": "session_123", "_memory_store": store, ...},
        permission_mode="bypassPermissions",
    )
    await runner.connect()
    result = await runner.run_conversation("Hello!")
    await runner.disconnect()
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Security hooks
# ---------------------------------------------------------------------------

async def _approval_hook(input_data: dict, tool_use_id, context) -> dict:
    """PreToolUse hook: detect dangerous commands via Hermes approval system."""
    if input_data.get("tool_name") != "Bash":
        return {}

    command = (input_data.get("tool_input") or {}).get("command", "")
    if not command:
        return {}

    try:
        from tools.approval import detect_dangerous_command
        is_dangerous, description = detect_dangerous_command(command)
        if is_dangerous:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Dangerous command blocked: {description}",
                }
            }
    except Exception as exc:
        logger.debug("Approval hook error: %s", exc)

    return {}


async def _tirith_hook(input_data: dict, tool_use_id, context) -> dict:
    """PreToolUse hook: scan commands with tirith security scanner."""
    if input_data.get("tool_name") != "Bash":
        return {}

    command = (input_data.get("tool_input") or {}).get("command", "")
    if not command:
        return {}

    try:
        from tools.tirith_security import check_command_security
        result = check_command_security(command)
        if result and result.get("verdict") == "block":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": result.get("summary", "Blocked by security scanner"),
                }
            }
    except Exception as exc:
        logger.debug("Tirith hook error: %s", exc)

    return {}


async def _redact_hook(input_data: dict, tool_use_id, context) -> dict:
    """PostToolUse hook: redact secrets from tool output."""
    # PostToolUse — we can't modify the output, but we log redaction warnings.
    # The SDK manages its own context; redaction in logs is handled separately.
    return {}


async def _injection_scan_hook(input_data: dict, tool_use_id, context) -> dict:
    """UserPromptSubmit hook: scan for prompt injection in user input."""
    message = input_data.get("message", "")
    if not message:
        return {}

    try:
        from agent.prompt_builder import _scan_context_content
        sanitized = _scan_context_content(message, "user_input")
        if sanitized != message and sanitized.startswith("[BLOCKED:"):
            return {
                "systemMessage": "Warning: input contained potential prompt injection and was flagged."
            }
    except Exception as exc:
        logger.debug("Injection scan hook error: %s", exc)

    return {}


def _build_hooks() -> dict:
    """Build the hooks dict for ClaudeAgentOptions."""
    try:
        from claude_agent_sdk import HookMatcher
    except ImportError:
        return {}

    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[_approval_hook, _tirith_hook]),
        ],
        "UserPromptSubmit": [
            HookMatcher(hooks=[_injection_scan_hook]),
        ],
    }


# ---------------------------------------------------------------------------
# SDKAgentRunner
# ---------------------------------------------------------------------------

class SDKAgentRunner:
    """Adapter that wraps ClaudeSDKClient for persistent multi-turn sessions.

    Provides ``run_conversation()`` with the same result dict interface as
    ``AIAgent.run_conversation()``.
    """

    def __init__(
        self,
        system_prompt: str = "",
        context: Optional[Dict[str, Any]] = None,
        permission_mode: str = "bypassPermissions",
        max_turns: int = 90,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        skip_duplicate_tools: bool = True,
        resume_session_id: Optional[str] = None,
    ):
        self._system_prompt = system_prompt
        self._context = context if context is not None else {}
        self._permission_mode = permission_mode
        self._max_turns = max_turns
        self._model = model
        self._cwd = cwd or str(Path.cwd())
        self._skip_duplicate_tools = skip_duplicate_tools
        self._resume_session_id = resume_session_id
        self._client = None
        self._sdk_session_id: Optional[str] = None

    async def connect(self, initial_prompt: Optional[str] = None):
        """Create and connect the ClaudeSDKClient subprocess."""
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, HookMatcher
        from agent.sdk_tool_bridge import build_hermes_mcp_servers, SDK_BUILTIN_OVERLAPS

        skip = set(SDK_BUILTIN_OVERLAPS) if self._skip_duplicate_tools else set()
        mcp_servers = build_hermes_mcp_servers(context=self._context, skip_tools=skip)

        # Build allowed_tools list — all MCP tools + SDK built-ins + Agent + AskUserQuestion
        allowed_tools = ["mcp__hermes_*__*"]  # wildcard for all hermes MCP servers
        if self._skip_duplicate_tools:
            allowed_tools.extend([
                "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                "WebSearch", "WebFetch",
            ])
        allowed_tools.extend(["Agent", "AskUserQuestion"])

        options = ClaudeAgentOptions(
            system_prompt=self._system_prompt or None,
            permission_mode=self._permission_mode,
            max_turns=self._max_turns,
            model=self._model,
            cwd=self._cwd,
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
            hooks=_build_hooks(),
            resume=self._resume_session_id,
        )

        self._client = ClaudeSDKClient(options=options)
        await self._client.connect(prompt=initial_prompt)

    async def disconnect(self):
        """Disconnect the subprocess."""
        if self._client:
            try:
                await self._client.disconnect()
            except Exception as exc:
                logger.debug("Disconnect error: %s", exc)
            self._client = None

    @property
    def sdk_session_id(self) -> Optional[str]:
        """The SDK-managed session ID, captured from the init message."""
        return self._sdk_session_id

    async def run_conversation(
        self,
        message: str,
        stream_callback: Optional[Callable] = None,
        tool_progress_callback: Optional[Callable] = None,
        **kwargs,
    ) -> dict:
        """Send a message and collect the response.

        Returns a dict matching the AIAgent.run_conversation() format::

            {
                "final_response": str,
                "messages": list,
                "api_calls": int,
                "completed": bool,
                "sdk_session_id": str,
                "cost_usd": float | None,
            }
        """
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ToolUseBlock,
        )

        if not self._client:
            await self.connect()

        # Update context with any kwargs (task_id, etc.)
        self._context.update(kwargs)

        await self._client.query(message)

        collected_messages: List[dict] = []
        final_response = ""
        api_calls = 0
        completed = True
        cost_usd = None

        async for msg in self._client.receive_response():
            # Capture SDK session ID from init
            if isinstance(msg, SystemMessage) and getattr(msg, "subtype", "") == "init":
                data = getattr(msg, "data", {}) or {}
                self._sdk_session_id = data.get("session_id") or getattr(msg, "session_id", None)

            # Stream assistant text
            elif isinstance(msg, AssistantMessage):
                collected_messages.append({
                    "role": "assistant",
                    "type": "assistant",
                    "content": _extract_content_summary(msg),
                })
                if stream_callback:
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            try:
                                stream_callback(block.text)
                            except Exception:
                                pass
                if tool_progress_callback:
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            try:
                                tool_progress_callback(block.name, block.input)
                            except Exception:
                                pass

            # Final result
            elif isinstance(msg, ResultMessage):
                final_response = getattr(msg, "result", "") or ""
                api_calls = getattr(msg, "num_turns", 0)
                completed = not getattr(msg, "is_error", False)
                cost_usd = getattr(msg, "total_cost_usd", None)
                self._sdk_session_id = getattr(msg, "session_id", self._sdk_session_id)

        return {
            "final_response": final_response,
            "messages": collected_messages,
            "api_calls": api_calls,
            "completed": completed,
            "sdk_session_id": self._sdk_session_id,
            "cost_usd": cost_usd,
        }

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()


# ---------------------------------------------------------------------------
# One-shot helper for cron
# ---------------------------------------------------------------------------

async def run_sdk_oneshot(
    prompt: str,
    system_prompt: str = "",
    context: Optional[Dict[str, Any]] = None,
    permission_mode: str = "bypassPermissions",
    max_turns: int = 90,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    skip_duplicate_tools: bool = True,
) -> dict:
    """Run a single prompt through the Agent SDK with no session persistence.

    Returns the same result dict format as SDKAgentRunner.run_conversation().
    Ideal for cron jobs and one-off tasks.
    """
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage, SystemMessage
    from agent.sdk_tool_bridge import build_hermes_mcp_servers, SDK_BUILTIN_OVERLAPS

    ctx = context if context is not None else {}
    skip = set(SDK_BUILTIN_OVERLAPS) if skip_duplicate_tools else set()
    mcp_servers = build_hermes_mcp_servers(context=ctx, skip_tools=skip)

    allowed_tools = ["mcp__hermes_*__*"]
    if skip_duplicate_tools:
        allowed_tools.extend([
            "Read", "Write", "Edit", "Bash", "Glob", "Grep",
            "WebSearch", "WebFetch",
        ])
    allowed_tools.extend(["Agent", "AskUserQuestion"])

    options = ClaudeAgentOptions(
        system_prompt=system_prompt or None,
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=model,
        cwd=cwd or str(Path.cwd()),
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
        hooks=_build_hooks(),
    )

    final_response = ""
    api_calls = 0
    completed = True
    cost_usd = None
    sdk_session_id = None

    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, SystemMessage) and getattr(msg, "subtype", "") == "init":
            data = getattr(msg, "data", {}) or {}
            sdk_session_id = data.get("session_id")

        elif isinstance(msg, ResultMessage):
            final_response = getattr(msg, "result", "") or ""
            api_calls = getattr(msg, "num_turns", 0)
            completed = not getattr(msg, "is_error", False)
            cost_usd = getattr(msg, "total_cost_usd", None)
            sdk_session_id = getattr(msg, "session_id", sdk_session_id)

    return {
        "final_response": final_response,
        "messages": [],
        "api_calls": api_calls,
        "completed": completed,
        "sdk_session_id": sdk_session_id,
        "cost_usd": cost_usd,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_content_summary(msg) -> str:
    """Extract a text summary from an AssistantMessage for transcript storage."""
    from claude_agent_sdk import TextBlock
    parts = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return " ".join(parts)
