#!/usr/bin/env python3
"""
MCP Replay Test Suite — replays real MCP trace calls against an okto-pulse MCP server.

Reads JSONL trace files, filters noise, captures dynamic IDs, and replays tool calls
via HTTP SSE transport. Validates responses against the original trace.

Usage:
    python mcp_replay.py --trace-file session_*.jsonl --api-key KEY [options]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRACE_TOOL_PREFIX = "okto_pulse_"

# Tools to always filter out (noise)
NOISE_TOOLS = {
    "okto_pulse_get_my_profile",       # repeated profile checks
}

# Error patterns that are known bugs in the trace server — skip entirely
SKIP_ERROR_PATTERNS = [
    r"name 'Board' is not defined",           # guideline creation bug
]

# Tools whose create responses contain IDs we should capture
CREATE_TOOLS_ID_FIELDS: Dict[str, List[str]] = {
    "okto_pulse_create_ideation": ["ideation.id"],
    "okto_pulse_create_refinement": ["refinement.id"],
    "okto_pulse_create_spec": ["spec.id"],
    "okto_pulse_derive_spec_from_refinement": ["spec.id"],
    "okto_pulse_derive_spec_from_ideation": ["spec.id"],
    "okto_pulse_create_card": ["card.id"],
    "okto_pulse_create_sprint": ["sprint.id"],
    "okto_pulse_add_business_rule": ["business_rule.id"],
    "okto_pulse_add_test_scenario": ["scenario.id"],
    "okto_pulse_add_decision": ["decision.id"],
    "okto_pulse_add_api_contract": ["api_contract.id"],
    "okto_pulse_add_spec_knowledge": ["knowledge.id"],
    "okto_pulse_add_screen_mockup": ["screen.id"],
    "okto_pulse_create_guideline": ["guideline.id"],
    "okto_pulse_ask_ideation_question": ["qa.id"],
    "okto_pulse_ask_ideation_choice_question": ["qa.id"],
    "okto_pulse_ask_refinement_question": ["qa.id"],
    "okto_pulse_ask_refinement_choice_question": ["qa.id"],
    "okto_pulse_ask_spec_question": ["qa.id"],
    "okto_pulse_ask_spec_choice_question": ["qa.id"],
    "okto_pulse_ask_sprint_question": ["qa.id"],
    "okto_pulse_ask_question": ["qa.id"],
    "okto_pulse_add_comment": ["comment.id"],
    "okto_pulse_add_choice_comment": ["comment.id"],
}

# Phase classification by tool name
PHASE_MAP: Dict[str, str] = {}
for _tool in [
    "okto_pulse_get_my_profile",
    "okto_pulse_list_my_boards",
    "okto_pulse_get_board",
    "okto_pulse_get_board_guidelines",
    "okto_pulse_update_my_profile",
]:
    PHASE_MAP[_tool] = "setup"

for _tool in [
    "okto_pulse_create_ideation",
    "okto_pulse_move_ideation",
    "okto_pulse_evaluate_ideation",
    "okto_pulse_ask_ideation_question",
    "okto_pulse_answer_ideation_question",
    "okto_pulse_ask_ideation_choice_question",
    "okto_pulse_get_ideation",
    "okto_pulse_get_ideation_context",
    "okto_pulse_get_ideation_history",
    "okto_pulse_list_ideations",
    "okto_pulse_delete_ideation_question",
]:
    PHASE_MAP[_tool] = "ideation"

for _tool in [
    "okto_pulse_create_refinement",
    "okto_pulse_move_refinement",
    "okto_pulse_ask_refinement_question",
    "okto_pulse_answer_refinement_question",
    "okto_pulse_ask_refinement_choice_question",
    "okto_pulse_add_refinement_knowledge",
    "okto_pulse_get_refinement",
    "okto_pulse_get_refinement_context",
    "okto_pulse_list_refinements",
    "okto_pulse_delete_refinement_question",
]:
    PHASE_MAP[_tool] = "refinement"

for _tool in [
    "okto_pulse_create_spec",
    "okto_pulse_derive_spec_from_refinement",
    "okto_pulse_derive_spec_from_ideation",
    "okto_pulse_update_spec",
    "okto_pulse_move_spec",
    "okto_pulse_add_business_rule",
    "okto_pulse_add_test_scenario",
    "okto_pulse_add_decision",
    "okto_pulse_add_api_contract",
    "okto_pulse_add_spec_knowledge",
    "okto_pulse_add_screen_mockup",
    "okto_pulse_annotate_mockup",
    "okto_pulse_create_spec_skill",
    "okto_pulse_get_spec",
    "okto_pulse_get_spec_context",
    "okto_pulse_get_spec_history",
    "okto_pulse_list_specs",
    "okto_pulse_list_test_scenarios",
    "okto_pulse_list_business_rules",
    "okto_pulse_list_api_contracts",
    "okto_pulse_update_test_scenario_status",
    "okto_pulse_ask_spec_question",
    "okto_pulse_answer_spec_question",
    "okto_pulse_ask_spec_choice_question",
    "okto_pulse_submit_spec_validation",
    "okto_pulse_submit_spec_evaluation",
    "okto_pulse_list_spec_evaluations",
    "okto_pulse_list_spec_validations",
    "okto_pulse_delete_spec_question",
    "okto_pulse_copy_mockups_to_card",
    "okto_pulse_copy_knowledge_to_card",
    "okto_pulse_copy_qa_to_card",
    "okto_pulse_link_card_to_spec",
    "okto_pulse_suggest_sprints",
]:
    PHASE_MAP[_tool] = "spec"

for _tool in [
    "okto_pulse_create_card",
    "okto_pulse_update_card",
    "okto_pulse_move_card",
    "okto_pulse_delete_card",
    "okto_pulse_get_card",
    "okto_pulse_list_cards_by_status",
    "okto_pulse_add_card_dependency",
    "okto_pulse_remove_card_dependency",
    "okto_pulse_get_card_dependencies",
    "okto_pulse_link_task_to_rule",
    "okto_pulse_link_task_to_tr",
    "okto_pulse_link_task_to_scenario",
    "okto_pulse_link_task_to_decision",
    "okto_pulse_link_task_to_contract",
    "okto_pulse_ask_question",
    "okto_pulse_answer_question",
    "okto_pulse_delete_question",
    "okto_pulse_add_comment",
    "okto_pulse_add_choice_comment",
    "okto_pulse_respond_to_choice",
    "okto_pulse_get_choice_responses",
    "okto_pulse_list_comments",
    "okto_pulse_update_comment",
    "okto_pulse_delete_comment",
    "okto_pulse_upload_attachment",
    "okto_pulse_list_attachments",
    "okto_pulse_delete_attachment",
    "okto_pulse_submit_task_validation",
    "okto_pulse_list_task_validations",
    "okto_pulse_get_task_context",
    "okto_pulse_get_task_conclusions",
]:
    PHASE_MAP[_tool] = "card"

for _tool in [
    "okto_pulse_create_sprint",
    "okto_pulse_update_sprint",
    "okto_pulse_move_sprint",
    "okto_pulse_get_sprint",
    "okto_pulse_get_sprint_context",
    "okto_pulse_list_sprints",
    "okto_pulse_assign_tasks_to_sprint",
    "okto_pulse_submit_sprint_evaluation",
    "okto_pulse_list_sprint_evaluations",
    "okto_pulse_ask_sprint_question",
    "okto_pulse_answer_sprint_question",
    "okto_pulse_delete_sprint_question",
]:
    PHASE_MAP[_tool] = "sprint"

for _tool in [
    "okto_pulse_get_analytics",
    "okto_pulse_list_blockers",
    "okto_pulse_get_activity_log",
    "okto_pulse_list_agents",
    "okto_pulse_list_board_members",
    "okto_pulse_list_guidelines",
    "okto_pulse_create_guideline",
    "okto_pulse_update_guideline",
    "okto_pulse_delete_guideline",
    "okto_pulse_link_guideline_to_board",
    "okto_pulse_unlink_guideline_from_board",
]:
    PHASE_MAP[_tool] = "admin"

# Scenario definitions — group tools into logical workflow scenarios
SCENARIOS: List[Dict[str, Any]] = [
    {
        "name": "setup",
        "phase": "setup",
        "description": "Board discovery and agent configuration",
        "tools": {
            "okto_pulse_get_my_profile",
            "okto_pulse_list_my_boards",
            "okto_pulse_get_board",
            "okto_pulse_get_board_guidelines",
            "okto_pulse_update_my_profile",
        },
    },
    {
        "name": "ideation-create",
        "phase": "ideation",
        "description": "Create ideations",
        "tools": {
            "okto_pulse_create_ideation",
            "okto_pulse_list_ideations",
        },
    },
    {
        "name": "ideation-lifecycle",
        "phase": "ideation",
        "description": "Ideation status transitions and evaluations",
        "tools": {
            "okto_pulse_move_ideation",
            "okto_pulse_evaluate_ideation",
        },
    },
    {
        "name": "ideation-qa",
        "phase": "ideation",
        "description": "Ideation Q&A, context, history",
        "tools": {
            "okto_pulse_ask_ideation_question",
            "okto_pulse_answer_ideation_question",
            "okto_pulse_ask_ideation_choice_question",
            "okto_pulse_get_ideation",
            "okto_pulse_get_ideation_context",
            "okto_pulse_get_ideation_history",
            "okto_pulse_delete_ideation_question",
        },
    },
    {
        "name": "refinement-create",
        "phase": "refinement",
        "description": "Create refinements for large-complexity ideations",
        "tools": {
            "okto_pulse_create_refinement",
            "okto_pulse_list_refinements",
        },
    },
    {
        "name": "refinement-enrichment",
        "phase": "refinement",
        "description": "Refinement Q&A, knowledge, status transitions, choice questions",
        "tools": {
            "okto_pulse_move_refinement",
            "okto_pulse_ask_refinement_question",
            "okto_pulse_answer_refinement_question",
            "okto_pulse_ask_refinement_choice_question",
            "okto_pulse_add_refinement_knowledge",
            "okto_pulse_get_refinement",
            "okto_pulse_get_refinement_context",
            "okto_pulse_delete_refinement_question",
        },
    },
    {
        "name": "spec-derive",
        "phase": "spec",
        "description": "Derive specs from refinements or ideations",
        "tools": {
            "okto_pulse_derive_spec_from_refinement",
            "okto_pulse_derive_spec_from_ideation",
        },
    },
    {
        "name": "spec-enrich",
        "phase": "spec",
        "description": "Enrich specs with rules, scenarios, decisions, contracts, mockups, skills, KB",
        "tools": {
            "okto_pulse_create_spec",
            "okto_pulse_update_spec",
            "okto_pulse_add_business_rule",
            "okto_pulse_add_test_scenario",
            "okto_pulse_add_decision",
            "okto_pulse_add_api_contract",
            "okto_pulse_add_spec_knowledge",
            "okto_pulse_add_screen_mockup",
            "okto_pulse_annotate_mockup",
            "okto_pulse_create_spec_skill",
            "okto_pulse_get_spec",
            "okto_pulse_get_spec_context",
            "okto_pulse_get_spec_history",
            "okto_pulse_list_specs",
            "okto_pulse_list_test_scenarios",
            "okto_pulse_list_business_rules",
            "okto_pulse_list_api_contracts",
            "okto_pulse_update_test_scenario_status",
            "okto_pulse_copy_mockups_to_card",
            "okto_pulse_copy_knowledge_to_card",
            "okto_pulse_copy_qa_to_card",
            "okto_pulse_link_card_to_spec",
            "okto_pulse_suggest_sprints",
        },
    },
    {
        "name": "spec-validate",
        "phase": "spec",
        "description": "Spec status transitions, validation gates, evaluations, Q&A",
        "tools": {
            "okto_pulse_move_spec",
            "okto_pulse_ask_spec_question",
            "okto_pulse_answer_spec_question",
            "okto_pulse_ask_spec_choice_question",
            "okto_pulse_submit_spec_validation",
            "okto_pulse_submit_spec_evaluation",
            "okto_pulse_list_spec_evaluations",
            "okto_pulse_list_spec_validations",
            "okto_pulse_delete_spec_question",
        },
    },
    {
        "name": "card-create",
        "phase": "card",
        "description": "Create task cards for specs",
        "tools": {
            "okto_pulse_create_card",
        },
    },
    {
        "name": "card-traceability",
        "phase": "card",
        "description": "Link tasks to rules, TRs, scenarios, decisions, contracts; manage dependencies",
        "tools": {
            "okto_pulse_link_task_to_rule",
            "okto_pulse_link_task_to_tr",
            "okto_pulse_link_task_to_scenario",
            "okto_pulse_link_task_to_decision",
            "okto_pulse_link_task_to_contract",
            "okto_pulse_add_card_dependency",
            "okto_pulse_remove_card_dependency",
            "okto_pulse_get_card_dependencies",
        },
    },
    {
        "name": "card-qa-validation",
        "phase": "card",
        "description": "Card Q&A, comments, attachments, task validation gates, context retrieval",
        "tools": {
            "okto_pulse_update_card",
            "okto_pulse_move_card",
            "okto_pulse_delete_card",
            "okto_pulse_get_card",
            "okto_pulse_list_cards_by_status",
            "okto_pulse_ask_question",
            "okto_pulse_answer_question",
            "okto_pulse_delete_question",
            "okto_pulse_add_comment",
            "okto_pulse_add_choice_comment",
            "okto_pulse_respond_to_choice",
            "okto_pulse_get_choice_responses",
            "okto_pulse_list_comments",
            "okto_pulse_update_comment",
            "okto_pulse_delete_comment",
            "okto_pulse_upload_attachment",
            "okto_pulse_list_attachments",
            "okto_pulse_delete_attachment",
            "okto_pulse_submit_task_validation",
            "okto_pulse_list_task_validations",
            "okto_pulse_get_task_context",
            "okto_pulse_get_task_conclusions",
        },
    },
    {
        "name": "sprint-management",
        "phase": "sprint",
        "description": "Create sprints, assign tasks, manage status",
        "tools": {
            "okto_pulse_create_sprint",
            "okto_pulse_update_sprint",
            "okto_pulse_move_sprint",
            "okto_pulse_get_sprint",
            "okto_pulse_get_sprint_context",
            "okto_pulse_list_sprints",
            "okto_pulse_assign_tasks_to_sprint",
        },
    },
    {
        "name": "sprint-evaluation",
        "phase": "sprint",
        "description": "Sprint evaluation gates and Q&A",
        "tools": {
            "okto_pulse_submit_sprint_evaluation",
            "okto_pulse_list_sprint_evaluations",
            "okto_pulse_ask_sprint_question",
            "okto_pulse_answer_sprint_question",
            "okto_pulse_delete_sprint_question",
        },
    },
    {
        "name": "analytics",
        "phase": "admin",
        "description": "Analytics queries, activity log, board inspection, guidelines",
        "tools": {
            "okto_pulse_get_analytics",
            "okto_pulse_list_blockers",
            "okto_pulse_get_activity_log",
            "okto_pulse_list_agents",
            "okto_pulse_list_board_members",
            "okto_pulse_list_guidelines",
            "okto_pulse_create_guideline",
            "okto_pulse_update_guideline",
            "okto_pulse_delete_guideline",
            "okto_pulse_link_guideline_to_board",
            "okto_pulse_unlink_guideline_from_board",
        },
    },
]

# Build tool→scenario lookup at import time
_SCENARIO_MAP: Dict[str, str] = {}
for _sc in SCENARIOS:
    for _tool in _sc["tools"]:
        _SCENARIO_MAP[_tool] = _sc["name"]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TraceEntry:
    """A single parsed trace line."""
    index: int
    ts: str
    session_id: str
    tool: str
    arguments: Dict[str, Any]
    is_error: bool
    response: Optional[Dict[str, Any]]
    error: Optional[Dict[str, Any]]
    duration_ms: Optional[float]
    phase: str = ""

    def __post_init__(self):
        self.phase = PHASE_MAP.get(self.tool, "other")
        self.scenario = _SCENARIO_MAP.get(self.tool, "other")


@dataclass
class Scenario:
    """A logical grouping of trace entries."""

    name: str  # e.g. "ideation-create"
    phase: str  # e.g. "ideation"
    description: str  # e.g. "Create 15 ideations"
    entry_indices: List[int] = field(default_factory=list)


@dataclass
class ReplayResult:
    """Result of replaying a single trace entry."""
    index: int
    tool: str
    status: str  # PASS | FAIL | SKIP | ERROR | ABSORBED
    expected_error: bool
    actual_error: bool
    duration_ms: Optional[float]
    elapsed_ms: float
    diff: Optional[str] = None
    expected_response: Optional[Dict[str, Any]] = None
    actual_response: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    scenario: str = ""  # which scenario this entry belongs to
    behavioral_status: str = ""  # PASS/FAIL in behavioral mode


# ---------------------------------------------------------------------------
# Variable Registry
# ---------------------------------------------------------------------------

class VariableRegistry:
    """Captures and substitutes dynamic IDs during replay."""

    def __init__(self) -> None:
        # Maps placeholder name → actual value
        self.vars: Dict[str, str] = {}
        # Maps original_id → placeholder_name (for substitution in arguments)
        self.id_map: Dict[str, str] = {}
        # Maps ORIGINAL trace IDs → LIVE replay IDs (bidirectional fix)
        self.trace_to_live: Dict[str, str] = {}
        # Counter for generated placeholders
        self._counter = 0

    def register(self, key: str, value: str) -> None:
        """Register a variable and create an ID mapping."""
        if value not in self.id_map:
            placeholder = f"{{{{{key}_{self._counter}}}}}"
            self._counter += 1
            self.vars[placeholder] = value
            self.id_map[value] = placeholder

    def register_trace_to_live(self, trace_id: str, live_id: str) -> None:
        """Register a mapping from an original trace ID to its live replay counterpart."""
        if trace_id and live_id and trace_id != live_id:
            self.trace_to_live[trace_id] = live_id
            # Also ensure the live ID is in id_map for reverse lookups
            if live_id not in self.id_map:
                placeholder = f"{{{{live_{self._counter}}}}}"
                self._counter += 1
                self.vars[placeholder] = live_id
                self.id_map[live_id] = placeholder

    def register_board(self, board_id: str) -> None:
        self.vars["{{board_id}}"] = board_id
        self.id_map[board_id] = "{{board_id}}"

    def register_agent(self, agent_id: str) -> None:
        self.vars["{{agent_id}}"] = agent_id
        self.id_map[agent_id] = "{{agent_id}}"

    def substitute_arguments(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Replace original IDs in arguments with values from registry."""
        result = deepcopy(args)
        substituted = self._deep_substitute(result)
        return substituted

    def _deep_substitute(self, obj: Any) -> Any:
        if isinstance(obj, str):
            # FIRST: check trace_to_live mapping (original trace ID → live ID)
            if obj in self.trace_to_live:
                return self.trace_to_live[obj]
            # SECOND: check id_map via placeholder resolution
            for orig_id, placeholder in self.id_map.items():
                actual = self.vars.get(placeholder, "")
                if actual and obj == orig_id:
                    return actual
            # THIRD: handle pipe-delimited or comma-delimited ID strings
            # e.g. "br_1e61d375|br_f0248171" or "ts_aaa,ts_bbb"
            substituted = self._substitute_delimited(obj)
            if substituted != obj:
                return substituted
            return obj
        if isinstance(obj, dict):
            return {k: self._deep_substitute(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._deep_substitute(item) for item in obj]
        return obj

    def _substitute_delimited(self, s: str) -> str:
        """Replace IDs within pipe-delimited or comma-delimited strings.

        Handles values like "br_1e61d375|br_f0248171" or "ts_aaa,ts_bbb".
        Returns the original string if no substitutions were made.
        """
        changed = False
        result = s
        # Try pipe-delimited first (more common in okto-pulse API)
        for orig_id, live_id in self.trace_to_live.items():
            if orig_id in result:
                result = result.replace(orig_id, live_id)
                changed = True
        if not changed:
            # Also check id_map via placeholder resolution for delimited strings
            for orig_id, placeholder in self.id_map.items():
                actual = self.vars.get(placeholder, "")
                if actual and orig_id in result:
                    result = result.replace(orig_id, actual)
                    changed = True
        return result if changed else s


# ---------------------------------------------------------------------------
# Trace Loader
# ---------------------------------------------------------------------------

def _detect_ghost_errors(entries: List[TraceEntry]) -> int:
    """Detect ghost errors in trace entries where is_error=False but response contains an error.

    The MCP trace recorder only checks HTTP status codes (200 = success) without
    inspecting response body for error indicators. This function promotes such
    entries to proper error entries.

    Returns the number of ghost errors detected and promoted.
    """
    count = 0
    for entry in entries:
        if entry.is_error:
            continue
        if not entry.response:
            continue
        text = _extract_text(entry.response)
        if text is None or not isinstance(text, str):
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and "error" in data:
            entry.is_error = True
            entry.error = {"message": str(data.get("error", ""))}
            count += 1
    return count


def load_trace_file(path: Path) -> List[TraceEntry]:
    """Load a JSONL trace file and parse each line."""
    entries: List[TraceEntry] = []
    with open(path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry = TraceEntry(
                index=idx,
                ts=data.get("ts", ""),
                session_id=data.get("session_id", ""),
                tool=data.get("tool", ""),
                arguments=data.get("arguments", {}),
                is_error=data.get("is_error", False),
                response=data.get("response"),
                error=data.get("error"),
                duration_ms=data.get("duration_ms"),
            )
            entries.append(entry)

    # Detect ghost errors — entries marked as success but contain error responses
    ghost_count = _detect_ghost_errors(entries)
    if ghost_count:
        logging.getLogger("mcp_replay").info(
            "Detected %d ghost errors in %s (promoted to proper errors)", ghost_count, path.name
        )

    return entries


def load_trace_files(paths: List[Path]) -> List[TraceEntry]:
    """Load one or more trace files, merging entries."""
    all_entries: List[TraceEntry] = []
    for p in paths:
        if p.is_file():
            all_entries.extend(load_trace_file(p))
        elif p.is_dir():
            for jsonl in sorted(p.glob("*.jsonl")):
                all_entries.extend(load_trace_file(jsonl))
    return all_entries


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

def should_skip_entry(entry: TraceEntry, prev_entries: List[TraceEntry]) -> bool:
    """Determine if a trace entry should be skipped during replay."""
    # Always skip noise tools (repeated profile checks)
    if entry.tool in NOISE_TOOLS:
        return True

    # Skip guideline creation failures (known server bug)
    if entry.is_error and entry.error:
        err_msg = entry.error.get("message", "")
        for pattern in SKIP_ERROR_PATTERNS:
            if re.search(pattern, err_msg):
                return True

    # Deduplicate consecutive identical failed calls (retry loops)
    # If the previous N entries are identical tool+args and all failed, skip duplicates
    if entry.is_error and len(prev_entries) >= 2:
        same_tool_count = 0
        for prev in reversed(prev_entries):
            if prev.tool == entry.tool and prev.arguments == entry.arguments and prev.is_error:
                same_tool_count += 1
            else:
                break
        # Skip if we've seen the same failed call 3+ times consecutively
        if same_tool_count >= 3:
            return True

    return False


def filter_trace(entries: List[TraceEntry]) -> List[TraceEntry]:
    """Filter trace entries, removing noise and keeping the golden path."""
    filtered: List[TraceEntry] = []
    for entry in entries:
        if should_skip_entry(entry, filtered):
            continue
        filtered.append(entry)
    return filtered


def filter_by_phase(entries: List[TraceEntry], phase: str) -> List[TraceEntry]:
    """Filter entries to a specific phase."""
    if not phase:
        return entries
    return [e for e in entries if e.phase == phase]


# ---------------------------------------------------------------------------
# MCP Client (Streamable HTTP Transport)
# ---------------------------------------------------------------------------

class MCPClient:
    """
    MCP client using the Streamable HTTP transport protocol.

    Protocol flow (3-step session-based):
      1. POST /mcp with initialize request → captures mcp-session-id from response header
      2. POST /mcp with notifications/initialized + session header → HTTP 202
      3. POST /mcp with tools/call + session header → SSE stream with JSON-RPC response
    """

    def __init__(self, host: str, port: int, api_key: str, path_prefix: str = "/mcp") -> None:
        if httpx is None:
            raise ImportError("httpx is required but not installed")
        self.base_url = f"http://{host}:{port}"
        self.path_prefix = path_prefix.rstrip("/")
        self.api_key = api_key
        self._client: Optional[httpx.Client] = None
        self._session_id: Optional[str] = None
        self._request_id = 0

    def _base_headers(self) -> Dict[str, str]:
        """Common headers for all MCP requests."""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-API-Key": self.api_key,
        }

    def _session_headers(self) -> Dict[str, str]:
        """Headers including session ID (required after initialization)."""
        headers = self._base_headers()
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    def _parse_sse_response(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Parse an SSE response body and extract the JSON-RPC message.

        SSE format:
            event: message
            data: {"jsonrpc": "2.0", "id": N, "result": {...}}
        """
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str:
                    try:
                        return json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
        # If no SSE data lines found, try parsing the whole body as JSON
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None

    def connect(self) -> None:
        """Initialize the MCP session (Step 1 + Step 2)."""
        self._client = httpx.Client(
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

        endpoint = f"{self.base_url}{self.path_prefix}"

        # Step 1: Send initialize request
        init_payload = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "replay-client", "version": "1.0"},
            },
            "id": 0,
        }

        resp = self._client.post(
            endpoint,
            json=init_payload,
            headers=self._base_headers(),
        )

        if resp.status_code >= 400:
            raise ConnectionError(
                f"Initialize failed (HTTP {resp.status_code}): {resp.text[:500]}"
            )

        # Capture session ID from response header
        self._session_id = resp.headers.get("mcp-session-id")
        if not self._session_id:
            # Fallback: try to extract from SSE response body
            parsed = self._parse_sse_response(resp.text)
            if parsed and "result" in parsed:
                # Some implementations return session info in the result
                pass
            # If still no session ID, proceed without it (some servers don't require it)
            pass

        # Step 2: Send initialized notification
        notif_payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }

        resp2 = self._client.post(
            endpoint,
            json=notif_payload,
            headers=self._session_headers(),
        )

        # Initialized notification typically returns 202 Accepted (no body)
        # but some implementations return 200 — both are fine
        if resp2.status_code >= 400:
            raise ConnectionError(
                f"Initialized notification failed (HTTP {resp2.status_code}): {resp2.text[:500]}"
            )

    def disconnect(self) -> None:
        if self._client:
            self._client.close()

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Tuple[Optional[Dict], Optional[Dict], float]:
        """
        Call an MCP tool via JSON-RPC (Step 3).

        Returns (response_body, error_body, elapsed_ms).
        response_body is None on error; error_body contains error details.
        """
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

        endpoint = f"{self.base_url}{self.path_prefix}"
        start = time.monotonic()
        try:
            resp = self._client.post(
                endpoint,
                json=payload,
                headers=self._session_headers(),
            )
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code >= 400:
                error_body = {"status_code": resp.status_code, "body": resp.text[:2000]}
                return None, error_body, elapsed_ms

            # Parse the SSE stream response
            body = self._parse_sse_response(resp.text)

            if body is None:
                error_body = {"type": "parse_error", "message": f"Could not parse response: {resp.text[:500]}"}
                return None, error_body, elapsed_ms

            # Check for JSON-RPC error
            if "error" in body:
                error_body = body["error"]
                return None, error_body, elapsed_ms

            # Success response — return the result field (MCP content format)
            result = body.get("result", {})

            # FIX 1: Detect content-level errors
            # The MCP server can return errors embedded in successful JSON-RPC responses:
            #   result.content[0].text = '{"error": "..."}'
            # These are NOT JSON-RPC errors (no top-level "error" key) but contain
            # application-level errors in the content text.
            content_error = _detect_content_level_error(result)
            if content_error is not None:
                # Return both the result (for ID capture) AND an error body
                return result, content_error, elapsed_ms

            return result, None, elapsed_ms

        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            error_body = {"type": type(exc).__name__, "message": str(exc)}
            return None, error_body, elapsed_ms


def _detect_content_level_error(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Check if an MCP result contains a content-level error.

    The MCP server returns errors in two ways:
    1. JSON-RPC error: top-level `"error": {...}` — handled by call_tool directly
    2. Content-level error: valid JSON-RPC with `result.content[0].text = '{"error": "..."}'`

    This function detects case 2 and returns a normalized error dict, or None if no error.
    """
    if result is None:
        return None

    text = _extract_text(result)
    if text is None or not isinstance(text, str):
        return None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    # Check for {"error": "..."} pattern (content-level error)
    if "error" in data:
        err_val = data["error"]
        return {
            "type": "content_error",
            "message": str(err_val),
            "raw": data,
        }

    return None


# ---------------------------------------------------------------------------
# Response Comparison
# ---------------------------------------------------------------------------

DYNAMIC_FIELDS = {
    "id", "created_at", "updated_at", "last_used_at",
    "session_id", "timestamp", "version",
    # Agent/user IDs change after reset — new agent gets different UUID
    "created_by", "asked_by", "author_id", "assigned_to_id",
    "answered_by", "updated_by", "actor_id",
    # Board ID changes after full reset
    "board_id",
}

# Fields whose values are always UUIDs that change between sessions
UUID_FIELDS = {
    "id", "created_by", "asked_by", "author_id", "assigned_to_id",
    "answered_by", "updated_by", "actor_id", "board_id",
    # Entity reference IDs
    "ideation_id", "refinement_id", "spec_id", "card_id",
    "sprint_id", "qa_id", "comment_id", "rule_id", "decision_id",
    "contract_id", "scenario_id", "knowledge_id", "screen_id",
    "guideline_id", "origin_task_id",
}


def _normalize_response(resp: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Deep-copy and normalize a response for comparison."""
    if resp is None:
        return None
    return deepcopy(resp)


def compare_responses(
    expected: Optional[Dict[str, Any]],
    actual: Optional[Dict[str, Any]],
    strict: bool = True,
) -> Tuple[bool, str]:
    """
    Compare expected vs actual response.

    Returns (match: bool, diff_description: str).
    """
    if expected is None and actual is None:
        return True, "both None"

    if expected is None or actual is None:
        return False, f"expected={expected is not None}, actual={actual is not None}"

    # Extract the text content from MCP response format
    exp_text = _extract_text(expected)
    act_text = _extract_text(actual)

    if exp_text is None and act_text is None:
        return True, "both have no text content"

    if exp_text is None or act_text is None:
        return False, f"exp_text={exp_text is not None}, act_text={act_text is not None}"

    # Try to parse as JSON for structured comparison
    try:
        exp_json = json.loads(exp_text) if isinstance(exp_text, str) else exp_text
    except (json.JSONDecodeError, TypeError):
        exp_json = None
    try:
        act_json = json.loads(act_text) if isinstance(act_text, str) else act_text
    except (json.JSONDecodeError, TypeError):
        act_json = None

    if exp_json and act_json:
        match, diff = _compare_dicts(exp_json, act_json, strict=strict)
        return match, diff

    # String comparison
    if exp_text == act_text:
        return True, "exact match"
    return False, f"text mismatch (expected len={len(str(exp_text))}, actual len={len(str(act_text))})"


def _extract_text(resp: Dict[str, Any]) -> Optional[Any]:
    """Extract the text content from an MCP response."""
    # Check structured_content (snake_case — trace format) and structuredContent (camelCase — live server)
    for key in ("structured_content", "structuredContent"):
        sc = resp.get(key, {})
        if isinstance(sc, dict):
            result = sc.get("result")
            if result is not None:
                return result

    # Fall back to content[0].text
    content = resp.get("content", [])
    if content and isinstance(content, list):
        first = content[0]
        if isinstance(first, dict):
            return first.get("text")
    return None


def _compare_dicts(
    expected: Dict[str, Any],
    actual: Dict[str, Any],
    strict: bool = True,
    path: str = "",
) -> Tuple[bool, str]:
    """Recursively compare two dicts, optionally ignoring dynamic fields and UUIDs."""
    if not isinstance(expected, dict):
        return False, f"expected is {type(expected).__name__}, not dict at '{path}'"
    if not isinstance(actual, dict):
        return False, f"actual is {type(actual).__name__}, not dict at '{path}'"
    all_keys = set(list(expected.keys()) + list(actual.keys()))

    # In non-strict mode, skip dynamic fields
    if not strict:
        all_keys -= DYNAMIC_FIELDS

    diffs: List[str] = []
    for key in sorted(all_keys):
        p = f"{path}.{key}" if path else key
        exp_val = expected.get(key)
        act_val = actual.get(key)

        if exp_val is None and act_val is not None:
            if strict:
                diffs.append(f"unexpected key '{p}' in actual")
            continue
        if act_val is None and exp_val is not None:
            if strict:
                diffs.append(f"missing key '{p}' in actual")
            continue

        # In non-strict mode, skip dynamic ID values for known ID fields at any depth
        if not strict and key in UUID_FIELDS:
            if _is_dynamic_id(str(exp_val or "")) and _is_dynamic_id(str(act_val or "")):
                continue  # Both are dynamic IDs — they'll differ after reset

        if isinstance(exp_val, dict) and isinstance(act_val, dict):
            sub_match, sub_diff = _compare_dicts(exp_val, act_val, strict, p)
            if not sub_match:
                diffs.append(f"{p}: {sub_diff}")
        elif isinstance(exp_val, list) and isinstance(act_val, list):
            if len(exp_val) != len(act_val):
                diffs.append(f"{p}: list length {len(exp_val)} vs {len(act_val)}")
            else:
                for i, (ev, av) in enumerate(zip(exp_val, act_val)):
                    if isinstance(ev, dict) and isinstance(av, dict):
                        sub_match, sub_diff = _compare_dicts(ev, av, strict, f"{p}[{i}]")
                        if not sub_match:
                            diffs.append(sub_diff)
                    elif ev != av:
                        # In non-strict mode, skip dynamic ID string comparisons
                        if not strict and _is_dynamic_id(str(ev)) and _is_dynamic_id(str(av)):
                            continue
                        diffs.append(f"{p}[{i}]: {repr(ev)[:80]} != {repr(av)[:80]}")
        else:
            if exp_val != act_val:
                # In non-strict mode, skip dynamic ID string comparisons at any depth
                if not strict and _is_dynamic_id(str(exp_val)) and _is_dynamic_id(str(act_val)):
                    continue
                diffs.append(f"{p}: {repr(exp_val)[:80]} != {repr(act_val)[:80]}")

    if diffs:
        return False, "; ".join(diffs[:5])  # limit diff output
    return True, "match"


def compare_errors(
    expected_error: Optional[Dict[str, Any]],
    actual_error: Optional[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Compare error structures.

    Handles multiple error formats:
    - JSON-RPC errors: {"type": "ToolError", "message": "..."}
    - Content-level errors from trace: {"message": "Failed to ..."}
    - Content-level errors from live: {"type": "content_error", "message": "...", "raw": {...}}
    """
    if expected_error is None and actual_error is None:
        return True, "both no error"

    if expected_error is None or actual_error is None:
        return False, f"expected_error={expected_error is not None}, actual_error={actual_error is not None}"

    # FIX 4: Normalize content-level errors for comparison
    # Extract the core message regardless of error format
    exp_msg = _extract_error_message(expected_error)
    act_msg = _extract_error_message(actual_error)

    if exp_msg and act_msg:
        # Check if expected message is contained in actual (or vice versa)
        if exp_msg in act_msg or act_msg in exp_msg:
            return True, "error messages match (partial)"
        # Check for key phrases — split on common delimiters
        exp_phrases = re.split(r"[,.:\s]+", exp_msg)
        matched = sum(1 for p in exp_phrases if len(p) > 3 and p.lower() in act_msg.lower())
        if matched >= 2:
            return True, f"error messages match ({matched} phrases)"
        # Fallback: check if the core error keywords overlap significantly
        exp_words = set(w.lower() for w in re.findall(r"[a-z]{4,}", exp_msg))
        act_words = set(w.lower() for w in re.findall(r"[a-z]{4,}", act_msg))
        if exp_words and act_words:
            overlap = len(exp_words & act_words) / len(exp_words | act_words)
            if overlap >= 0.5:
                return True, f"error messages match (word overlap {overlap:.0%})"
        return False, f"error message mismatch: expected '{exp_msg[:80]}' got '{act_msg[:80]}'"

    # If we couldn't extract messages, fall back to type comparison
    exp_type = expected_error.get("type", "")
    act_type = actual_error.get("type", "")
    if exp_type and act_type:
        # content_error is a wrapper — compare underlying types if possible
        if exp_type == "content_error" or act_type == "content_error":
            return True, "both are content-level errors (types normalized)"
        if exp_type != act_type:
            return False, f"error type '{exp_type}' != '{act_type}'"

    return True, "error comparison OK"


def _extract_error_message(error: Dict[str, Any]) -> str:
    """Extract the core error message from any error format.

    Handles:
    - {"message": "..."} — ghost error from trace
    - {"type": "ToolError", "message": "..."} — JSON-RPC error
    - {"type": "content_error", "message": "...", "raw": {...}} — live content error
    """
    # Direct message field
    msg = error.get("message", "")
    if msg:
        return str(msg)

    # Try raw field (content_error wrapper)
    raw = error.get("raw", {})
    if isinstance(raw, dict):
        inner_err = raw.get("error", "")
        if inner_err:
            return str(inner_err)

    return ""


def compare_behavioral(
    expected_error: bool,
    actual_error: bool,
) -> Tuple[bool, str]:
    """Compare only the success/failure outcome, ignoring content.

    Returns (match: bool, description: str).
    """
    if expected_error and actual_error:
        return True, "error→error (behavioral match)"
    if not expected_error and not actual_error:
        return True, "success→success (behavioral match)"
    if expected_error and not actual_error:
        return False, "expected error but got success (behavior change)"
    if not expected_error and actual_error:
        return False, "expected success but got error (regression)"
    return False, "behavioral mismatch"


# ---------------------------------------------------------------------------
# Board Reset
# ---------------------------------------------------------------------------

def reset_board(client: MCPClient, board_id: str, logger: logging.Logger) -> bool:
    """Delete all entities on a board to prepare for replay."""
    logger.info("Resetting board %s ...", board_id)
    ok = True

    # Delete sprints (need spec context)
    for tool in [
        ("okto_pulse_list_specs", {"board_id": board_id}),
    ]:
        result, err, _ = client.call_tool(tool[0], tool[1])
        if err:
            logger.warning("Failed to list specs during reset: %s", err)
            continue

        # Extract spec IDs from response
        text = _extract_text(result) if result else None
        if text:
            try:
                data = json.loads(text) if isinstance(text, str) else text
                specs = data.get("specs", [])
                for spec in specs:
                    spec_id = spec.get("id", "")
                    if not spec_id:
                        continue

                    # Delete cards linked to this spec
                    cards_result, _, _ = client.call_tool("okto_pulse_list_cards_by_status", {
                        "board_id": board_id, "spec_id": spec_id, "status": "", "limit": 200,
                    })
                    if cards_result:
                        cards_text = _extract_text(cards_result)
                        if cards_text:
                            try:
                                cards_data = json.loads(cards_text) if isinstance(cards_text, str) else cards_text
                                for card in cards_data.get("cards", []):
                                    card_id = card.get("id", "")
                                    if card_id:
                                        _, del_err, _ = client.call_tool("okto_pulse_delete_card", {
                                            "board_id": board_id, "card_id": card_id,
                                        })
                                        if del_err:
                                            logger.debug("Failed to delete card %s: %s", card_id, del_err)
                            except json.JSONDecodeError:
                                pass

                    # Delete spec
                    _, del_err, _ = client.call_tool("okto_pulse_delete_spec", {
                        "board_id": board_id, "spec_id": spec_id,
                    })
                    if del_err:
                        logger.debug("Failed to delete spec %s: %s", spec_id, del_err)

            except json.JSONDecodeError:
                pass

    # Delete ideations
    result, err, _ = client.call_tool("okto_pulse_list_ideations", {
        "board_id": board_id, "limit": 200,
    })
    if not err and result:
        text = _extract_text(result)
        if text:
            try:
                data = json.loads(text) if isinstance(text, str) else text
                for ideation in data.get("ideations", []):
                    ideation_id = ideation.get("id", "")
                    if ideation_id:
                        _, del_err, _ = client.call_tool("okto_pulse_delete_ideation", {
                            "board_id": board_id, "ideation_id": ideation_id,
                        })
                        if del_err:
                            logger.debug("Failed to delete ideation %s: %s", ideation_id, del_err)
            except json.JSONDecodeError:
                pass

    # Delete guidelines
    result, err, _ = client.call_tool("okto_pulse_get_board_guidelines", {
        "board_id": board_id,
    })
    if not err and result:
        text = _extract_text(result)
        if text:
            try:
                data = json.loads(text) if isinstance(text, str) else text
                for gl in data.get("guidelines", []):
                    gl_id = gl.get("id", "")
                    if gl_id:
                        _, del_err, _ = client.call_tool("okto_pulse_delete_guideline", {
                            "board_id": board_id, "guideline_id": gl_id,
                        })
                        if del_err:
                            logger.debug("Failed to delete guideline %s: %s", gl_id, del_err)
            except json.JSONDecodeError:
                pass

    logger.info("Board reset complete")
    return ok


# ---------------------------------------------------------------------------
# Variable Capture from Response
# ---------------------------------------------------------------------------

def capture_ids_from_response(
    tool_name: str,
    response: Optional[Dict[str, Any]],
    registry: VariableRegistry,
) -> None:
    """Extract IDs from create_* responses and register them."""
    if response is None:
        return

    text = _extract_text(response)
    if not text:
        return

    try:
        data = json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        return

    # Navigate to the nested entity based on tool
    fields = CREATE_TOOLS_ID_FIELDS.get(tool_name, [])
    for field_path in fields:
        parts = field_path.split(".")
        obj = data
        for part in parts:
            if isinstance(obj, dict):
                obj = obj.get(part)
            else:
                obj = None
                break
        if isinstance(obj, str) and obj:
            # Generate a key name from the tool
            base = tool_name.replace("okto_pulse_", "").replace("_", "_")
            registry.register(base, obj)


def _extract_ids_from_response(
    tool_name: str,
    response: Optional[Dict[str, Any]],
) -> List[str]:
    """Extract entity IDs from a response using CREATE_TOOLS_ID_FIELDS.

    Returns a list of ID strings found at the expected field paths.
    """
    if response is None:
        return []

    text = _extract_text(response)
    if not text:
        return []

    try:
        data = json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        return []

    ids: List[str] = []
    fields = CREATE_TOOLS_ID_FIELDS.get(tool_name, [])
    for field_path in fields:
        parts = field_path.split(".")
        obj = data
        for part in parts:
            if isinstance(obj, dict):
                obj = obj.get(part)
            else:
                obj = None
                break
        if isinstance(obj, str) and obj:
            ids.append(obj)
    return ids


def _extract_sub_entity_ids(
    response: Optional[Dict[str, Any]],
    field_path: str,
    prefix: str,
) -> List[str]:
    """Extract sub-entity IDs (tr_*, br_*, api_*, dec_*, ts_*) from a nested field.

    Walks the response JSON to find the field at `field_path`, then scans all
    string values under it for IDs matching the given prefix pattern.

    Returns a sorted list of matching IDs so that positional zip-mapping is stable.
    """
    if response is None:
        return []

    text = _extract_text(response)
    if not text:
        return []

    try:
        data = json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        return []

    # Navigate to the field path
    parts = field_path.split(".")
    obj = data
    for part in parts:
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return []

    # Collect all IDs matching prefix from the subtree
    ids: List[str] = _collect_ids_from_tree(obj, prefix)
    # Sort for stable positional mapping (server returns in insertion order;
    # both trace and live should have the same order)
    return sorted(ids)


def _collect_ids_from_tree(obj: Any, prefix: str) -> List[str]:
    """Recursively collect all string values matching a prefix from a tree."""
    found: List[str] = []
    if isinstance(obj, dict):
        for val in obj.values():
            found.extend(_collect_ids_from_tree(val, prefix))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_collect_ids_from_tree(item, prefix))
    elif isinstance(obj, str) and obj.startswith(prefix):
        found.append(obj)
    return found


def _map_trace_to_live_ids(
    tool_name: str,
    trace_response: Optional[Dict[str, Any]],
    live_response: Optional[Dict[str, Any]],
    registry: VariableRegistry,
) -> None:
    """Map original trace entity IDs to their live replay counterparts.

    After a create_* call succeeds, the trace response contains the ORIGINAL
    entity IDs (from the recorded session), while the live response contains
    NEWLY GENERATED IDs. This function creates bidirectional mappings so that
    subsequent tool calls referencing original trace IDs get substituted with
    live IDs.

    Special handling for update_spec: TR IDs are generated inside the spec
    response body, not as top-level entity fields. We extract them from both
    responses and map positionally.
    """
    # --- Special handler for update_spec: map TR IDs ---
    if tool_name == "okto_pulse_update_spec":
        trace_tr_ids = _extract_sub_entity_ids(trace_response, "spec.technical_requirements", "tr_")
        live_tr_ids = _extract_sub_entity_ids(live_response, "spec.technical_requirements", "tr_")
        for orig_id, live_id in zip(trace_tr_ids, live_tr_ids):
            registry.register_trace_to_live(orig_id, live_id)
        # Also map functional requirement IDs if present (fr_* pattern — future-proofing)
        trace_fr_ids = _extract_sub_entity_ids(trace_response, "spec.functional_requirements", "fr_")
        live_fr_ids = _extract_sub_entity_ids(live_response, "spec.functional_requirements", "fr_")
        for orig_id, live_id in zip(trace_fr_ids, live_fr_ids):
            registry.register_trace_to_live(orig_id, live_id)

    # --- Standard handler: map top-level entity IDs from CREATE_TOOLS_ID_FIELDS ---
    if tool_name not in CREATE_TOOLS_ID_FIELDS:
        return

    trace_ids = _extract_ids_from_response(tool_name, trace_response)
    live_ids = _extract_ids_from_response(tool_name, live_response)

    for trace_id, live_id in zip(trace_ids, live_ids):
        registry.register_trace_to_live(trace_id, live_id)


def capture_first_create_id(
    tool_name: str,
    response: Optional[Dict[str, Any]],
    registry: VariableRegistry,
) -> None:
    """For the first create call of a type, capture the ID with a special key."""
    if response is None:
        return

    text = _extract_text(response)
    if not text:
        return

    try:
        data = json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        return

    # Walk the dict to find UUIDs
    _walk_and_capture(data, registry, tool_name)


def _walk_and_capture(obj: Any, registry: VariableRegistry, context: str) -> None:
    """Walk a response object and capture UUID-like strings."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key == "id" and isinstance(val, str) and _is_uuid(val):
                registry.register(f"{context}_{key}", val)
            else:
                _walk_and_capture(val, registry, context)
    elif isinstance(obj, list):
        for item in obj:
            _walk_and_capture(item, registry, context)


def _is_uuid(s: str) -> bool:
    return bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", s))


def _is_dynamic_id(s: str) -> bool:
    """Check if a string is a dynamic entity ID that changes between sessions.

    Covers both UUIDs and server-generated slugs like br_*, dec_*, ts_*, sm_*, tr_*.
    """
    if _is_uuid(s):
        return True
    # Server-generated entity IDs with prefixes
    if re.match(r"^(br_|dec_|ts_|sm_|qa_|gl_|skn_|cntrt_|tr_)[0-9a-f]+$", s):
        return True
    return False


# ---------------------------------------------------------------------------
# Replay Engine
# ---------------------------------------------------------------------------

class ReplayEngine:
    """Orchestrates trace replay against a live MCP server."""

    def __init__(
        self,
        entries: List[TraceEntry],
        client: MCPClient,
        registry: VariableRegistry,
        stop_on_fail: bool = False,
        strict: bool = True,
        behavioral: bool = False,
        log_file: Optional[str] = None,
    ) -> None:
        self.entries = entries
        self.client = client
        self.registry = registry
        self.stop_on_fail = stop_on_fail
        self.strict = strict
        self.behavioral = behavioral
        self.results: List[ReplayResult] = []
        self.logger = logging.getLogger("replay")

        # Detailed log file handler
        if log_file:
            fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.logger.addHandler(fh)

    def run(self) -> List[ReplayResult]:
        """Execute the replay."""
        self.logger.info("Starting replay of %d trace entries", len(self.entries))

        for entry in self.entries:
            if self.stop_on_fail and any(r.status in ("FAIL", "ERROR") for r in self.results):
                self.logger.info("Stopping on first failure at entry #%d", entry.index)
                break

            result = self._replay_entry(entry)
            self.results.append(result)
            self._log_result(result)

        return self.results

    def _replay_entry(self, entry: TraceEntry) -> ReplayResult:
        """Replay a single trace entry."""
        # Substitute variables in arguments
        args = self.registry.substitute_arguments(entry.arguments)

        # Send the tool call
        start = time.monotonic()
        actual_response, actual_error, elapsed_ms = self.client.call_tool(entry.tool, args)
        elapsed_ms_total = (time.monotonic() - start) * 1000

        # Capture IDs from response for future substitution
        # Even content-level errors may contain entity data worth capturing
        if actual_response:
            capture_ids_from_response(entry.tool, actual_response, self.registry)

            # Map original trace IDs → live replay IDs (only for non-error responses)
            if not actual_error and entry.response:
                _map_trace_to_live_ids(entry.tool, entry.response, actual_response, self.registry)

        # Determine expected vs actual error state
        expected_error = entry.is_error
        actual_has_error = actual_error is not None

        # FIX 2: Clear 4-path decision tree for comparison
        match = False
        diff = ""
        absorbed = False  # learning artifact flag
        if expected_error and actual_has_error:
            # Both errored — compare errors (error-vs-error = PASS if similar)
            match, diff = compare_errors(entry.error, actual_error)
            status = "PASS" if match else "FAIL"
        elif expected_error and not actual_has_error:
            # Expected error but got success — learning artifact in behavioral mode
            absorbed = True
            status = "ABSORBED" if self.behavioral else "FAIL"
            diff = "expected error but got success (learning artifact)"
            match = False
        elif not expected_error and actual_has_error:
            # Unexpected error on what should be a success call
            status = "FAIL"
            diff = f"unexpected error: {actual_error}"
            match = False
        else:
            # Both success — compare responses with non-strict mode (default)
            match, diff = compare_responses(entry.response, actual_response, strict=self.strict)
            status = "PASS" if match else "FAIL"

        # Always compute behavioral comparison alongside exact comparison
        behavioral_match, behavioral_diff = compare_behavioral(expected_error, actual_has_error)
        behavioral_status = "PASS" if behavioral_match else "FAIL"

        # In behavioral mode, use behavioral_status as the primary status
        # but preserve ABSORBED for learning artifacts (error→success)
        if self.behavioral and not absorbed:
            status = behavioral_status
            diff = behavioral_diff
            match = behavioral_match

        return ReplayResult(
            index=entry.index,
            tool=entry.tool,
            status=status,
            expected_error=expected_error,
            actual_error=actual_has_error,
            duration_ms=entry.duration_ms,
            elapsed_ms=elapsed_ms_total,
            diff=diff if not match else None,
            expected_response=_normalize_response(entry.response),
            actual_response=_normalize_response(actual_response) if not expected_error else None,
            error_message=json.dumps(actual_error)[:500] if actual_error and not expected_error else None,
            scenario=entry.scenario,
            behavioral_status=behavioral_status,
        )

    def _log_result(self, result: ReplayResult) -> None:
        """Log a replay result to console and file."""
        if result.status == "PASS":
            color = "\033[92m"       # green
        elif result.status == "ABSORBED":
            color = "\033[93m"      # amber/yellow
        else:
            color = "\033[91m"      # red
        reset = "\033[0m"

        tool_short = result.tool.replace("okto_pulse_", "")
        status_label = "ABSB" if result.status == "ABSORBED" else result.status
        line = f"{color}[{status_label}]#{result.index:03d} {tool_short:<45s} {result.elapsed_ms:.0f}ms{reset}"
        if result.diff:
            line += f"  — {result.diff[:120]}"

        # Console output
        print(line, flush=True)

        # File log
        self.logger.debug(
            "Entry #%d tool=%s status=%s elapsed=%.1fms diff=%s",
            result.index, result.tool, result.status, result.elapsed_ms,
            (result.diff or "")[:200],
        )


# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------

def generate_report(
    results: List[ReplayResult],
    output_path: str,
    trace_file: str,
) -> None:
    """Generate a JSON report file."""
    total = len(results)
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    errors = sum(1 for r in results if r.status == "ERROR")
    skipped = sum(1 for r in results if r.status == "SKIP")
    absorbed = sum(1 for r in results if r.status == "ABSORBED")
    total_ms = sum(r.elapsed_ms for r in results)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_file": str(trace_file),
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "absorbed": absorbed,
            "pass_rate": f"{(passed / total * 100):.1f}%" if total else "N/A",
            "effective_pass_rate": f"{((passed + absorbed) / total * 100):.1f}%" if total else "N/A",
            "total_elapsed_ms": round(total_ms, 1),
        },
        "results": [],
    }

    for r in results:
        entry = {
            "index": r.index,
            "tool": r.tool,
            "status": r.status,
            "elapsed_ms": round(r.elapsed_ms, 1),
            "expected_error": r.expected_error,
            "actual_error": r.actual_error,
        }
        if r.diff:
            entry["diff"] = r.diff
        if r.error_message:
            entry["error_message"] = r.error_message
        report["results"].append(entry)

    # Add scenario grouping
    scenarios = group_results_by_scenario(results)
    report["scenarios"] = []
    for sc_def in SCENARIOS:
        sc_name = sc_def["name"]
        if sc_name in scenarios:
            data = scenarios[sc_name]
            report["scenarios"].append({
                "name": data["name"],
                "total": data["total"],
                "passed": data["passed"],
                "failed": data["failed"],
                "pass_rate": f"{(data['passed'] / data['total'] * 100):.1f}%" if data['total'] else "N/A",
                "behavioral_pass_rate": f"{(data['behavioral_passed'] / data['total'] * 100):.1f}%" if data['total'] else "N/A",
                "entry_indices": data["entries"],
            })
    # Include any "other" entries not in SCENARIOS
    if "other" in scenarios:
        data = scenarios["other"]
        report["scenarios"].append({
            "name": "other",
            "total": data["total"],
            "passed": data["passed"],
            "failed": data["failed"],
            "pass_rate": f"{(data['passed'] / data['total'] * 100):.1f}%" if data['total'] else "N/A",
            "behavioral_pass_rate": f"{(data['behavioral_passed'] / data['total'] * 100):.1f}%" if data['total'] else "N/A",
            "entry_indices": data["entries"],
        })

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


def group_results_by_scenario(results: List[ReplayResult]) -> Dict[str, Dict[str, Any]]:
    """Group replay results by scenario and compute per-scenario statistics."""
    scenarios: Dict[str, Dict[str, Any]] = {}
    for r in results:
        name = r.scenario or "other"
        if name not in scenarios:
            scenarios[name] = {
                "name": name,
                "total": 0,
                "passed": 0,
                "failed": 0,
                "absorbed": 0,
                "behavioral_passed": 0,
                "behavioral_failed": 0,
                "entries": [],
            }
        scenarios[name]["total"] += 1
        if r.status == "PASS":
            scenarios[name]["passed"] += 1
        elif r.status == "FAIL":
            scenarios[name]["failed"] += 1
        elif r.status == "ABSORBED":
            scenarios[name]["absorbed"] += 1
        if r.behavioral_status == "PASS":
            scenarios[name]["behavioral_passed"] += 1
        elif r.behavioral_status == "FAIL":
            scenarios[name]["behavioral_failed"] += 1
        scenarios[name]["entries"].append(r.index)
    return scenarios


# ---------------------------------------------------------------------------
# Phase Detection for Golden Path Cleanup
# ---------------------------------------------------------------------------

def clean_golden_path(entries: List[TraceEntry]) -> List[TraceEntry]:
    """
    Clean the trace to produce a golden path by removing:
    - Initial setup/discovery calls (lines 1-7)
    - Agent reconfiguration (lines 8-11)
    - Failed guideline attempts (all create_guideline that error)
    - Retry loops in ideation status transitions (keep only successful path)
    """
    cleaned: List[TraceEntry] = []

    # Track which ideations have been moved through each status to avoid duplicates
    seen_moves: Dict[str, set] = {}  # ideation_id -> set of statuses successfully moved

    skip_setup_until_first_create = True

    for entry in entries:
        # Skip setup calls until we see the first create_ideation
        if skip_setup_until_first_create:
            if entry.tool == "okto_pulse_create_ideation":
                skip_setup_until_first_create = False
                cleaned.append(entry)
                continue
            else:
                continue

        # Skip guideline creation failures (known server bug)
        if entry.tool == "okto_pulse_create_guideline" and entry.is_error:
            err_msg = entry.error.get("message", "") if entry.error else ""
            if "Board" in err_msg or "not defined" in err_msg:
                continue

        # Skip list_guidelines failures
        if entry.tool == "okto_pulse_list_guidelines" and entry.is_error:
            continue

        # Handle ideation move deduplication
        if entry.tool == "okto_pulse_move_ideation":
            ideation_id = entry.arguments.get("ideation_id", "")
            target_status = entry.arguments.get("status", "")

            if ideation_id:
                if ideation_id not in seen_moves:
                    seen_moves[ideation_id] = set()

                # Skip failed moves (these are retry attempts)
                if entry.is_error:
                    continue

                # Skip duplicate successful moves to same status
                if target_status in seen_moves[ideation_id]:
                    continue

                seen_moves[ideation_id].add(target_status)

        cleaned.append(entry)

    return cleaned


# ---------------------------------------------------------------------------
# Full Reset (SSH-based data wipe + re-init)
# ---------------------------------------------------------------------------

class FullResetError(Exception):
    """Raised when a full-reset step fails."""


def _run_ssh(ssh_host: str, command: str, logger: logging.Logger, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a command on the remote server via SSH. Returns CompletedProcess."""
    full_cmd = f"ssh {ssh_host} {command}"
    logger.info("SSH: %s", command.strip())
    result = subprocess.run(
        ["bash", "-c", full_cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.stdout.strip():
        logger.debug("SSH stdout: %s", result.stdout.strip()[:500])
    if result.stderr.strip():
        logger.debug("SSH stderr: %s", result.stderr.strip()[:500])
    return result


def _wait_for_healthy(ssh_host: str, container_name: str, logger: logging.Logger, timeout: int = 120) -> bool:
    """Poll container health until healthy or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _run_ssh(
            ssh_host,
            f'docker inspect --format="{{{{.State.Health.Status}}}}" {container_name} 2>/dev/null || '
            f'docker inspect --format="{{{{.State.Status}}}}" {container_name}',
            logger,
            timeout=10,
        )
        status = result.stdout.strip().strip('"').strip("{}").strip()
        logger.debug("Container %s health: %s", container_name, status)
        if "healthy" in status.lower():
            return True
        # If the container doesn't have a healthcheck, accept "running"
        if result.returncode == 0 and "running" in status.lower() and "health" not in status.lower():
            logger.info("Container %s is running (no healthcheck detected)", container_name)
            return True
        time.sleep(3)
    logger.error("Container %s did not become healthy within %ds", container_name, timeout)
    return False


def _capture_api_key(ssh_host: str, data_dir: str, logger: logging.Logger) -> Optional[str]:
    """Try to read the API key from the SQLite database on the remote server."""
    db_path = f"{data_dir}/data/pulse.db"

    # Write a Python script to host, then docker cp into container and run
    # This avoids all quoting issues through multiple shell layers
    import tempfile
    script_content = (
        "import sqlite3\n"
        "conn = sqlite3.connect('/data/data/pulse.db')\n"
        "row = conn.execute('SELECT api_key FROM agents LIMIT 1').fetchone()\n"
        "if row: print(row[0])\n"
    )

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(script_content)
        local_script = f.name

    try:
        # Copy script to remote host
        remote_script = "/tmp/_get_api_key.py"
        scp_cmd = f"scp {local_script} {ssh_host}:{remote_script}"
        subprocess.run(["bash", "-c", scp_cmd], capture_output=True, text=True, timeout=10)

        # Copy into container and run
        cp_cmd = f"docker cp {remote_script} okto-pulse:/tmp/_get_api_key.py"
        _run_ssh(ssh_host, cp_cmd, logger, timeout=10)

        run_cmd = "docker exec okto-pulse python3 /tmp/_get_api_key.py"
        result = _run_ssh(ssh_host, run_cmd, logger, timeout=15)
        if result.returncode == 0:
            key = result.stdout.strip()
            if key and key.startswith("dash_"):
                return key
            if key:
                return key

        # Fallback: try sqlite3 CLI via docker exec
        cmd = 'docker exec okto-pulse sqlite3 "/data/data/pulse.db" "SELECT api_key FROM agents LIMIT 1;"'
        result = _run_ssh(ssh_host, cmd, logger, timeout=15)
        if result.returncode == 0:
            key = result.stdout.strip()
            if key and key.startswith("dash_"):
                return key
            if key:
                return key

    finally:
        try:
            os.unlink(local_script)
        except OSError:
            pass

    # Fallback: try direct SSH sqlite3 (may fail on root-owned files)
    cmd = f'sqlite3 "{db_path}" "SELECT api_key FROM agents LIMIT 1;"'
    result = _run_ssh(ssh_host, cmd, logger, timeout=15)
    if result.returncode == 0:
        key = result.stdout.strip()
        if key and key.startswith("dash_"):
            return key
    # Fallback: try without the dash_ prefix filter
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def full_reset(
    ssh_host: str,
    data_dir: str,
    container_name: str,
    wipe_traces: bool,
    logger: logging.Logger,
) -> str:
    """
    Perform a complete data wipe of the remote okto-pulse instance.

    Steps:
      1. SSH and wipe data directories (data/*, boards/*, global/*, uploads/*)
      2. Optionally wipe mcp_traces/
      3. Restart the container via SSH docker restart
      4. Wait for container to be healthy
      5. Run okto-pulse init inside the container
      6. Capture the new API key from stdout or DB

    Returns the new API key string.
    """
    logger.info("=" * 60)
    logger.info("FULL RESET — wiping remote instance")
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # Step 1: Start container briefly to wipe data from inside (root)
    # ------------------------------------------------------------------
    logger.info("Step 1/7: Starting container '%s' for data wipe...", container_name)
    result = _run_ssh(ssh_host, f"docker start {container_name}", logger, timeout=30)
    if result.returncode != 0:
        raise FullResetError(
            f"Failed to start container (exit {result.returncode}). "
            f"stderr: {result.stderr.strip()[:300]}"
        )

    # ------------------------------------------------------------------
    # Step 2: Wipe data directories via docker exec (root inside container)
    # ------------------------------------------------------------------
    logger.info("Step 2/7: Wiping data directories...")

    # The container mounts host data_dir to /data, so we wipe /data/* from inside
    wipe_targets = [
        "/data/data/*",
        "/data/boards/*",
        "/data/global/*",
        "/data/uploads/*",
    ]
    if wipe_traces:
        wipe_targets.append("/data/mcp_traces/*")

    # Wipe each directory individually using docker exec with explicit args (no shell glob)
    for target in wipe_targets:
        # Use find to delete contents without relying on shell glob expansion
        base_dir = target.replace("/*", "")
        wipe_single = f"docker exec {container_name} find {base_dir} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +"
        result = _run_ssh(ssh_host, wipe_single, logger, timeout=30)
        if result.returncode != 0:
            logger.warning("Wipe of %s had issues (non-fatal): %s", target, result.stderr.strip()[:200])
    logger.info("Data directories wiped successfully")

    # ------------------------------------------------------------------
    # Step 3: Stop container before restart cycle
    # ------------------------------------------------------------------
    logger.info("Step 3/7: Stopping container for clean restart...")
    result = _run_ssh(ssh_host, f"docker stop {container_name}", logger, timeout=30)
    if result.returncode != 0:
        raise FullResetError(
            f"Failed to stop container (exit {result.returncode}). "
            f"stderr: {result.stderr.strip()[:300]}"
        )
    logger.info("Container stopped")

    # ------------------------------------------------------------------
    # Step 4: Start container fresh
    # ------------------------------------------------------------------
    logger.info("Step 4/7: Starting container '%s'...", container_name)
    result = _run_ssh(ssh_host, f"docker start {container_name}", logger, timeout=30)
    if result.returncode != 0:
        raise FullResetError(
            f"Failed to start container (exit {result.returncode}). "
            f"stderr: {result.stderr.strip()[:300]}"
        )
    logger.info("Container started")

    # ------------------------------------------------------------------
    # Step 5: Wait for healthy
    # ------------------------------------------------------------------
    logger.info("Step 5/7: Waiting for container to become healthy...")
    if not _wait_for_healthy(ssh_host, container_name, logger, timeout=120):
        raise FullResetError(f"Container '{container_name}' did not become healthy in time")
    logger.info("Container is healthy")

    # Give it a moment after health check for internal services to settle
    time.sleep(5)

    # ------------------------------------------------------------------
    # Step 6: Run okto-pulse init
    # ------------------------------------------------------------------
    logger.info("Step 6/7: Running 'okto-pulse init' inside container...")
    result = _run_ssh(
        ssh_host,
        f"docker exec {container_name} okto-pulse init",
        logger,
        timeout=60,
    )
    if result.returncode != 0:
        raise FullResetError(
            f"'okto-pulse init' failed (exit {result.returncode}). "
            f"stdout: {result.stdout.strip()[:300]} stderr: {result.stderr.strip()[:300]}"
        )
    logger.info("okto-pulse init completed")

    # ------------------------------------------------------------------
    # Step 7: Capture new API key
    # ------------------------------------------------------------------
    logger.info("Step 7/7: Capturing new API key...")

    # Try to parse from init output first
    api_key = None
    init_output = (result.stdout + "\n" + result.stderr).strip()
    for line in init_output.split("\n"):
        match = re.search(r"API\s*Key[:\s]+(dash_[^\s]+)", line)
        if match:
            api_key = match.group(1)
            logger.info("Found API key in init output: %s", api_key)
            break

    # Fallback: read from SQLite DB
    if not api_key:
        logger.info("API key not found in init output, reading from database...")
        api_key = _capture_api_key(ssh_host, data_dir, logger)

    if not api_key:
        raise FullResetError(
            "Could not capture API key. Check init output and database manually.\n"
            f"Init output:\n{init_output[:1000]}"
        )

    logger.info("New API key captured: %s", api_key)
    logger.info("=" * 60)
    logger.info("FULL RESET COMPLETE")
    logger.info("=" * 60)

    return api_key


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MCP Replay Test Suite — replay trace calls against okto-pulse MCP server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python mcp_replay.py --trace-file session_*.jsonl --api-key KEY
  python mcp_replay.py --trace-file traces/ --api-key KEY --reset --stop-on-fail
  python mcp_replay.py --trace-file session.jsonl --api-key KEY --dry-run --phase ideation
        """,
    )

    parser.add_argument(
        "--trace-file", required=True,
        help="Path to trace JSONL file or directory of files",
    )
    parser.add_argument(
        "--api-key", required=True,
        help="Okto-pulse API key for authentication",
    )
    parser.add_argument(
        "--host", default="localhost",
        help="MCP server host (default: localhost)",
    )
    parser.add_argument(
        "--port", type=int, default=9101,
        help="MCP server port (default: 9101)",
    )
    parser.add_argument(
        "--path-prefix", default="/mcp",
        help="MCP route path prefix (default: /mcp)",
    )
    parser.add_argument(
        "--board-name", default="My Board",
        help='Board name to use (default: "My Board")',
    )
    parser.add_argument(
        "--stop-on-fail", action="store_true",
        help="Stop on first validation failure",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Reset board before replay (delete all entities)",
    )
    parser.add_argument(
        "--report", default="replay_report.json",
        help="Output report file path (default: replay_report.json)",
    )
    parser.add_argument(
        "--log-file", default="replay.log",
        help="Detailed log file (default: replay.log)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and validate trace without sending calls",
    )
    parser.add_argument(
        "--phase", default="",
        choices=["setup", "ideation", "refinement", "spec", "card", "sprint", "admin"],
        help="Replay only specific phase",
    )
    parser.add_argument(
        "--strict", action="store_true", default=False,
        help="Strict response comparison (default: False — non-strict after reset)",
    )
    parser.add_argument(
        "--no-strict", dest="strict", action="store_false",
        help="Lenient response comparison (ignore dynamic fields; default after reset)",
    )
    parser.add_argument(
        "--behavioral", action="store_true", default=False,
        help="Behavioral mode: only compare success/failure outcomes, not response content",
    )

    # Full-reset group
    reset_group = parser.add_argument_group("full-reset options")
    reset_group.add_argument(
        "--full-reset", action="store_true",
        help="Full data wipe via SSH: wipe data dirs, restart container, re-init, capture new API key",
    )
    reset_group.add_argument(
        "--ssh-host", default="maheidem@192.168.31.154",
        help="SSH host for full-reset (default: maheidem@192.168.31.154)",
    )
    reset_group.add_argument(
        "--data-dir", default="/home/maheidem/docker/okto-pulse/data",
        help="Remote data directory path (default: /home/maheidem/docker/okto-pulse/data)",
    )
    reset_group.add_argument(
        "--container-name", default="okto-pulse",
        help="Docker container name to restart (default: okto-pulse)",
    )
    reset_group.add_argument(
        "--wipe-traces", action="store_true",
        help="When used with --full-reset, also wipe mcp_traces/ directory",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(console)

    logger = logging.getLogger("mcp_replay")

    # Resolve trace file paths
    trace_paths: List[Path] = []
    for p_str in args.trace_file.split(","):
        p = Path(p_str.strip())
        if p.exists():
            trace_paths.append(p)
        else:
            logger.error("Trace path not found: %s", p)
            return 1

    # Load and parse traces
    logger.info("Loading trace files...")
    all_entries = load_trace_files(trace_paths)
    logger.info("Loaded %d raw trace entries", len(all_entries))

    if not all_entries:
        logger.error("No trace entries found")
        return 1

    # Filter noise
    filtered = filter_trace(all_entries)
    logger.info("After noise filtering: %d entries", len(filtered))

    # Clean to golden path
    golden = clean_golden_path(filtered)
    logger.info("After golden path cleanup: %d entries", len(golden))

    # Phase filter
    if args.phase:
        golden = filter_by_phase(golden, args.phase)
        logger.info("After phase filter (%s): %d entries", args.phase, len(golden))

    if not golden:
        logger.error("No entries to replay after filtering")
        return 1

    # Dry run mode
    if args.dry_run:
        logger.info("=== DRY RUN — no calls will be sent ===")
        phase_counts: Dict[str, int] = {}
        for e in golden:
            phase_counts[e.phase] = phase_counts.get(e.phase, 0) + 1
        logger.info("Phase breakdown:")
        for phase, count in sorted(phase_counts.items()):
            logger.info("  %-12s %d calls", phase, count)

        error_count = sum(1 for e in golden if e.is_error)
        success_count = len(golden) - error_count
        logger.info("Expected: %d success, %d errors (expected)", success_count, error_count)

        # Scenario breakdown
        scenario_counts: Dict[str, int] = {}
        for e in golden:
            scenario_counts[e.scenario] = scenario_counts.get(e.scenario, 0) + 1
        logger.info("Scenario breakdown:")
        for sc_def in SCENARIOS:
            sc_name = sc_def["name"]
            if sc_name in scenario_counts:
                logger.info("  %-25s %d calls", sc_name, scenario_counts[sc_name])
        if "other" in scenario_counts:
            logger.info("  %-25s %d calls", "other", scenario_counts["other"])

        # Print first 5 and last 5 entries
        for e in golden[:5]:
            tool_short = e.tool.replace("okto_pulse_", "")
            err_marker = " [EXPECTED ERROR]" if e.is_error else ""
            logger.info("  #%d %-45s%s", e.index, tool_short, err_marker)
        if len(golden) > 10:
            logger.info("  ... (%d entries omitted) ...", len(golden) - 10)
        for e in golden[-5:]:
            tool_short = e.tool.replace("okto_pulse_", "")
            err_marker = " [EXPECTED ERROR]" if e.is_error else ""
            logger.info("  #%d %-45s%s", e.index, tool_short, err_marker)

        return 0

    # Full reset: wipe remote data, restart container, re-init, capture new API key
    if args.full_reset:
        logger.info("Performing full reset before replay...")
        try:
            new_api_key = full_reset(
                ssh_host=args.ssh_host,
                data_dir=args.data_dir,
                container_name=args.container_name,
                wipe_traces=args.wipe_traces,
                logger=logger,
            )
            args.api_key = new_api_key
            logger.info("Full reset complete. New API key: %s", new_api_key)
        except FullResetError as exc:
            logger.error("Full reset failed: %s", exc)
            return 1

    # Connect to MCP server
    logger.info("Connecting to MCP server at %s:%d ...", args.host, args.port)
    registry = VariableRegistry()
    client = MCPClient(args.host, args.port, args.api_key, path_prefix=args.path_prefix)

    try:
        client.connect()
        logger.info("Connected to MCP server")
    except Exception as exc:
        logger.error("Failed to connect to MCP server: %s", exc)
        return 1

    try:
        # Resolve board_id by listing boards
        logger.info("Resolving board ID for '%s' ...", args.board_name)
        boards_result, boards_err, _ = client.call_tool("okto_pulse_list_my_boards", {})
        if boards_err:
            logger.error("Failed to list boards: %s", boards_err)
            return 1

        text = _extract_text(boards_result)
        board_id = None
        if text:
            try:
                data = json.loads(text) if isinstance(text, str) else text
                for board in data.get("boards", []):
                    if board.get("name") == args.board_name:
                        board_id = board.get("id")
                        break
                # Fallback: use first board
                if not board_id and data.get("boards"):
                    board_id = data["boards"][0].get("id")
            except json.JSONDecodeError:
                pass

        if not board_id:
            logger.error("Board '%s' not found", args.board_name)
            return 1

        registry.register_board(board_id)

        # Also map the original trace board_id → live board_id
        # Find the original board_id from the first trace entry that has one
        for entry in golden:
            orig_board = entry.arguments.get("board_id", "")
            if orig_board and orig_board != board_id:
                registry.register_trace_to_live(orig_board, board_id)
                logger.info("Mapped trace board_id %s → live board_id %s", orig_board, board_id)
                break

        logger.info("Using board: %s (%s)", args.board_name, board_id)

        # Reset board if requested
        if args.reset:
            reset_board(client, board_id, logger)

        # Run replay
        engine = ReplayEngine(
            entries=golden,
            client=client,
            registry=registry,
            stop_on_fail=args.stop_on_fail,
            strict=args.strict,
            behavioral=args.behavioral,
            log_file=args.log_file,
        )
        results = engine.run()

        # Summary
        total = len(results)
        passed = sum(1 for r in results if r.status == "PASS")
        failed = sum(1 for r in results if r.status == "FAIL")
        errors = sum(1 for r in results if r.status == "ERROR")
        absorbed = sum(1 for r in results if r.status == "ABSORBED")

        print()
        print("=" * 60)
        summary_line = f"REPLAY COMPLETE: {total} calls, {passed} passed, {failed} failed, {errors} errors"
        if absorbed:
            summary_line += f", {absorbed} absorbed"
        print(summary_line)
        if total:
            effective_pass = passed + absorbed
            print(f"Pass rate: {(passed / total * 100):.1f}% (effective: {(effective_pass / total * 100):.1f}% with {absorbed} absorbed)")
        print("=" * 60)

        # Scenario summary
        print()
        print("=" * 60)
        print("SCENARIO RESULTS")
        print("=" * 60)
        scenarios = group_results_by_scenario(results)
        for sc_def in SCENARIOS:
            sc_name = sc_def["name"]
            if sc_name not in scenarios:
                continue
            data = scenarios[sc_name]
            exact_rate = (data['passed'] / data['total'] * 100) if data['total'] else 0
            behav_rate = (data['behavioral_passed'] / data['total'] * 100) if data['total'] else 0
            color = "\033[92m" if exact_rate >= 90 else "\033[93m" if exact_rate >= 70 else "\033[91m"
            reset_c = "\033[0m"
            print(f"  {color}{sc_name:<25s} {data['passed']:>3d}/{data['total']:<4d} exact "
                  f"{behav_rate:>5.1f}% behavioral{reset_c}")
        # Print "other" if any
        if "other" in scenarios:
            data = scenarios["other"]
            exact_rate = (data['passed'] / data['total'] * 100) if data['total'] else 0
            behav_rate = (data['behavioral_passed'] / data['total'] * 100) if data['total'] else 0
            color = "\033[92m" if exact_rate >= 90 else "\033[93m" if exact_rate >= 70 else "\033[91m"
            reset_c = "\033[0m"
            print(f"  {color}{data['name']:<25s} {data['passed']:>3d}/{data['total']:<4d} exact "
                  f"{behav_rate:>5.1f}% behavioral{reset_c}")
        print("=" * 60)

        # Behavioral mode summary (always shown for comparison)
        total_bh_pass = sum(1 for r in results if r.behavioral_status == "PASS")
        total_bh_fail = sum(1 for r in results if r.behavioral_status == "FAIL")
        print(f"\nBehavioral pass rate: {total_bh_pass}/{total} ({(total_bh_pass/total*100):.1f}%)")

        # Generate report
        generate_report(results, args.report, args.trace_file)
        logger.info("Report written to %s", args.report)

        return 0 if failed == 0 and errors == 0 else 1

    finally:
        client.disconnect()


if __name__ == "__main__":
    sys.exit(main())
