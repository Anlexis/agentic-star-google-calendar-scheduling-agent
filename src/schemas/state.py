"""AgentCore Platform v1.0 - CMN-C2-235 Google Workspace Calendar Agent state."""

# State must be a flat TypedDict - never Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization; Pydantic objects (and nested
# dict/list containers) are not msgpack-safe. Extend AgentState with
# agent-specific fields only, and declare every domain field NotRequired[...]
# (fields are absent until their producer node writes them). calendar_payload /
# calendar_config / caller_event / redaction_flags are dicts/lists at the point
# of use but are stored in State as JSON strings via to_json/from_json below.
# Do NOT add credentials, secrets, or Pydantic models. The Google Calendar
# integration token is NEVER stored here - it is read via ctx.secrets in
# CallCalendarApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Google Workspace Calendar agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only calendar-workflow fields are added below, all NotRequired (state
    contract). All values are JSON/msgpack-serializable primitives - the
    Google Calendar integration token is NEVER stored here (accessed via
    ctx.secrets).
    """

    # Caller-supplied target hint (event id from input_context), validated by
    # PreProcessNode against the bounded event-id shape. Never inferred;
    # resolution to a Google Calendar event id is explicit-only.
    event_hint: NotRequired[str]
    event_id: NotRequired[str]  # resolved/created Google Calendar event id

    # Validated structured event fields from input_context.event (summary /
    # start / end / location / description), collected and screened by
    # PreProcessNode and carried into the inner graph over the context bridge
    # (JSON string, msgpack-safe). Authoritative over free-text inference:
    # the request-text channel is rewritten by the framework's masking
    # heuristics, this channel is not.
    caller_event: NotRequired[Optional[str]]

    # ValidateInput (deterministic input scan)
    # JSON list[str] of patterns redacted from the text before logging
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferCalendarFields
    event_summary: NotRequired[str]  # event title (Google Calendar `summary`)
    # JSON - assembled Google Calendar API v3 event body (stored as a JSON
    # string, not a native dict; (de)serialize via to_json/from_json).
    calendar_payload: NotRequired[Optional[str]]

    # config/config.yaml `google_calendar:` section forwarded by
    # _parent_config() and injected by the inner graph's
    # _extra_initial_state() (JSON string).
    calendar_config: NotRequired[Optional[str]]

    # CallCalendarApi
    event_ref: NotRequired[str]  # human-readable reference (gcal://calendars/<cal>/events/<id>)

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
