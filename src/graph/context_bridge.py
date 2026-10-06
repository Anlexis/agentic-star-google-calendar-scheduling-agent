"""AgentCore Platform v1.0 - caller-context bridge across the graph boundary."""

# Why this exists: GraphNode.execute() invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)` and does NOT forward
# outer state fields, so the caller's validated event data (the event-id hint
# and the structured event fields collected from input_context by
# PreProcessNode) would never reach the inner workflow on its own. The
# sanctioned subclass hooks bridge it:
#
#   CalendarWorkflowGraphNode.extract_input(state)   [runs BEFORE subgraph.invoke]
#       -> set_caller_event_context({"event_hint": ..., "event": ...})
#   CalendarWorkflowGraph._extra_initial_state()     [runs INSIDE subgraph.invoke]
#       -> seeds {"event_hint": ..., "caller_event": <JSON>}
#
# What crosses the bridge is the VALIDATED caller contract produced by
# PreProcessNode - the hint already passed the bounded event-id shape check
# and every event field passed its bounds + content screens - never the raw
# request body.
#
# The alternative, smuggling the data inside the validated_input JSON, is not
# reliable here: the framework masks that field at node boundaries, and real
# calendar data trips the masking heuristics - a Title Case event title
# ("Quarterly Business Review") or a hyphenated numeric event id is rewritten
# to "[MASKED]", so the calendar write would carry corrupted caller data.
# This channel is not masked.
#
# A ContextVar keeps the hand-off correct per thread/task, so concurrent
# invocations in one process cannot see each other's event data.

from contextvars import ContextVar
from typing import Any

_CALLER_EVENT_CONTEXT: ContextVar["dict[str, Any] | None"] = ContextVar("cmn_c2_235_caller_event_context", default=None)


def set_caller_event_context(event_context: "dict[str, Any] | None") -> None:
    """Stash the validated caller event data for the imminent inner-graph invoke."""
    _CALLER_EVENT_CONTEXT.set(dict(event_context) if event_context else {})


def get_caller_event_context() -> "dict[str, Any]":
    """Read (without consuming) the stashed event data; {} when none was set."""
    return _CALLER_EVENT_CONTEXT.get() or {}
