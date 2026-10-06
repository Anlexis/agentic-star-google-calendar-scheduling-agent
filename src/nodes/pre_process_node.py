"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: serialize the caller's request (raw NL text + validated
event data) into a single JSON string in `validated_input`, which the GraphNode
(`main` slot) hands to the inner calendar workflow graph. Business validation
happens inside the inner graph's ValidateInputNode - this node owns the
CALLER-DATA CONTRACT: the empty-guard, the HTML/length sanitize, the
template-owned injection screen, and the field-by-field validation of
`input_context` (every accepted value is bounded; a malformed value is refused
with an error that names the field and never echoes the value).

Why input_context carries the event data: the pipeline's other input channel
(the request text serialized into validated_input) is rewritten by the
framework's PII masking heuristics at every node boundary - a Title Case event
title ("Quarterly Business Review") or a hyphenated numeric event id arrives
at the calendar write as "[MASKED]". Structured caller data therefore travels
through input_context, which is not masked, and this node screens that channel
itself: prompt-injection content (post-parse, keys included) is refused, and
contact identifiers (email addresses, phone numbers, government/card numbers)
found by the platform PII detector are refused naming the field. Person-name
heuristic findings are deliberately EXCLUDED from that refusal: event titles
and locations legitimately contain Title Case proper nouns, and the name
heuristic matches any two capitalized words (see docs/02_design.md
"Caller-data contract").
"""

import json

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import DATETIME_VALUE_RE, EVENT_ID_RE, find_injection, sanitize_query

# input_context keys that may carry an explicit target event id (caller-supplied
# only - a target is never inferred here). First present key wins.
_HINT_KEYS = ("event_id", "event_hint", "calendar_event_id")

# The structured event contract: input_context["event"] may carry these fields,
# each bounded below. An unknown field inside `event` is refused (silently
# dropping a misspelled field would write an event missing data the caller
# supplied); the refusal names the container, never the unknown key itself.
_EVENT_TEXT_BOUNDS = {"summary": 200, "location": 500, "description": 2000}
_EVENT_TIME_KEYS = ("start", "end")
_EVENT_KEYS = tuple(_EVENT_TEXT_BOUNDS) + _EVENT_TIME_KEYS

# detect_pii finding types refused in caller event fields. The `name` /
# `name_jp` heuristics are excluded by design (documented above): a calendar
# title is legitimately two Title Case words.
_CONTACT_PII_TYPES = frozenset({"email", "phone_jp", "phone_us", "ssn_us", "my_number_jp", "credit_card"})


def _iter_context_strings(value: Any) -> "list[str]":
    """Every string in a parsed input_context - keys AND values, at any depth.

    The injection screen runs post-parse over this list, so a payload hidden in
    a mapping KEY, nested one level down, or JSON-escaped on the wire (parsed
    back to its real characters by then) is screened exactly like a top-level
    value.
    """
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.append(key)
            found.extend(_iter_context_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_iter_context_strings(item))
    return found


class PreProcessNode(FunctionNode):
    """Validate + serialize caller input for the inner workflow graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner Google Calendar call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not user_input or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # Caller context must be a mapping; anything else is refused without
        # being echoed (fail closed - never guess at a malformed contract).
        if input_context is None:
            input_context = {}
        if not isinstance(input_context, dict):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: input_context must be an object"],
            }

        # Template-owned injection screen. The request text is screened RAW
        # (before the HTML strip, so a control token cannot be half-eaten into
        # an ordinary-looking request), and input_context is screened
        # post-parse over every key and string value at any depth. Refusals
        # name the pattern type only - hostile content is never echoed.
        findings = find_injection(user_input)
        if findings:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: user_input contains disallowed content ({findings[0]})"],
            }
        for text in _iter_context_strings(input_context):
            findings = find_injection(text)
            if findings:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PreProcessNode: input_context contains disallowed content ({findings[0]})"],
                }

        # Strip HTML markup + cap length before JSON serialization.
        sanitized_input = sanitize_query(user_input.strip())

        # Target event is caller-supplied and never inferred here: an explicit
        # id from input_context, validated against the bounded event-id shape.
        # A present-but-malformed value (wrong type, wrong charset, over-length)
        # is a hard error naming the FIELD, never the value - silently dropping
        # it could update or cancel a different event than the caller intended.
        event_hint = ""
        for key in _HINT_KEYS:
            value = input_context.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PreProcessNode: input_context.{key} must be a string event id"],
                }
            value = value.strip()
            if not value:
                continue  # blank = absent
            if not EVENT_ID_RE.match(value):
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PreProcessNode: input_context.{key} is not a valid event id"],
                }
            event_hint = value
            break

        # Structured event fields: validated field-by-field against explicit
        # bounds; datetimes must be explicit ISO-like values; free-text fields
        # are screened for contact identifiers (refused naming the field -
        # the value never appears in the error). Absent data degrades to the
        # text-inference baseline in the inner graph.
        caller_event, problem = self._validate_event(input_context.get("event"))
        if problem:
            return {"status": AgentStatus.ERROR.value, "error_log": [problem]}

        validated_input = json.dumps({"text": sanitized_input, "event_hint": event_hint})

        # Audit the shaped request - presence signals only, not the raw text.
        emit_trace_event(
            "pre_process_complete",
            {"has_event_hint": bool(event_hint), "has_caller_event": bool(caller_event)},
            state,
        )

        return {
            "validated_input": validated_input,
            "event_hint": event_hint,
            "caller_event": to_json(caller_event) if caller_event else None,
            "status": AgentStatus.SUCCESS.value,
        }

    # -- caller event validation ----------------------------------------------

    def _validate_event(self, event: Any) -> "tuple[dict[str, str], str]":
        """Validate input_context.event -> (accepted fields, "" | problem).

        Fail closed: wrong container type, an unsupported field, a wrong-typed
        or over-bound value, a malformed datetime, or a contact identifier in
        free text each refuse with a field-naming error. Values are never
        echoed; the unsupported-field refusal does not echo the key either
        (a field NAME is caller-controlled text too).
        """
        if event is None:
            return {}, ""
        if not isinstance(event, dict):
            return {}, "PreProcessNode: input_context.event must be an object"
        unknown = [key for key in event if key not in _EVENT_KEYS]
        if unknown:
            return {}, "PreProcessNode: input_context.event contains an unsupported field"

        accepted: dict[str, str] = {}
        for key, max_len in _EVENT_TEXT_BOUNDS.items():
            value = event.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                return {}, f"PreProcessNode: input_context.event.{key} must be a string"
            value = sanitize_query(value.strip(), max_length=max_len + 1)
            if not value:
                continue  # blank = absent
            if len(value) > max_len:
                return {}, f"PreProcessNode: input_context.event.{key} exceeds {max_len} characters"
            contact_types = sorted({f["type"] for f in detect_pii(value)} & _CONTACT_PII_TYPES)
            if contact_types:
                return {}, (
                    f"PreProcessNode: input_context.event.{key} carries a contact identifier "
                    f"({contact_types[0]}) - remove it and resubmit"
                )
            accepted[key] = value
        for key in _EVENT_TIME_KEYS:
            value = event.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                return {}, f"PreProcessNode: input_context.event.{key} must be a string"
            value = value.strip()
            if not value:
                continue
            if not DATETIME_VALUE_RE.match(value):
                return {}, (
                    f"PreProcessNode: input_context.event.{key} must be an explicit "
                    "ISO-like datetime (YYYY-MM-DD [HH:MM[:SS]])"
                )
            accepted[key] = value
        return accepted, ""
