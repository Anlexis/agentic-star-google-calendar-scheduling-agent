"""AgentCore Platform v1.0 - inner workflow Step 3: InferCalendarFields.

Assembles a validated Google Calendar API v3 event body for the classified
intent from two sources, in a fixed precedence order:

1. The VALIDATED caller event fields (state `caller_event`, collected from
   input_context.event by the outer pre_process node and carried over the
   context bridge). These are authoritative: they arrive exactly as the caller
   supplied them, while the request-text channel below is rewritten by the
   framework's masking heuristics (a Title Case title arrives as "[MASKED]").
2. Free-text inference over the (redacted) request text, filling only the
   fields the caller did not supply: the event id, title, start/end datetimes,
   location, and description are extracted only when written explicitly - an
   unresolved id is left empty rather than invented (never update/cancel the
   wrong event; the executor surfaces the miss as status=error), datetimes are
   accepted only in an explicit ISO-like shape, and a missing field is
   omitted, never invented. Deterministic v1 - no LLM (docs/02_design.md
   "v1 Implementation Note").
"""

import re

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json
from src.services.security import DATETIME_VALUE_RE, EVENT_ID_RE

# A Google Calendar event id: URL-safe identifier (no spaces). Single
# definition in src/services/security.py - the same bounded shape pre_process
# validates caller-supplied hints against.
_ID_SHAPE_RE = EVENT_ID_RE
# Explicit event-id mention in the request text, EN or JA
# ("event id abc123" / "event ID: xyz-1" / "イベントID abc123").
_ID_IN_TEXT_RE = re.compile(
    r"(?:event)\s+(?:id|code)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{3,63})"
    r"|(?:イベントID|イベント番号)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{3,63})",
    re.IGNORECASE,
)
# Quoted event title: titled "Foo" / called "Foo". Curly quotes as \u escapes so
# the source stays pure ASCII (push-safe).
_TITLE_QUOTED_RE = re.compile(r'(?:titled|called|named|for)\s+["“]([^"”\n]+)["”]', re.IGNORECASE)
# "Key: value" field lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs, as \u escapes (push-safe).
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# Explicit ISO-like datetime value - the same bounded shape pre_process
# accepts for caller-supplied start/end (single definition in security.py).
_DATETIME_VALUE_RE = DATETIME_VALUE_RE
# Field-line keys mapped to the Google Calendar event resource.
_ID_KEYS = ("event id", "event code", "id", "イベントid")
_TITLE_KEYS = ("title", "summary", "event name", "name", "subject", "件名", "タイトル")
_START_KEYS = ("start", "start time", "starts", "from", "begin", "begins", "開始")
_END_KEYS = ("end", "end time", "ends", "until", "finish", "終了")
_LOCATION_KEYS = ("location", "place", "room", "venue", "where", "場所")
_DESC_KEYS = ("description", "notes", "details", "agenda", "memo", "備考", "説明")


class InferCalendarFieldsNode(FunctionNode):
    """Merge caller fields + extracted entities into the API v3 event body."""

    # Inner domain node - derives fields from already-validated data; the
    # external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "") or ""
        event_hint = state.get("event_hint", "") or ""
        caller_event = from_json(state.get("caller_event"), {}) or {}

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferCalendarFieldsNode: missing validated_input"],
            }

        fields = self._parse_fields(text)
        event_id = self._resolve_event_id(text, event_hint, fields)
        event_summary = str(caller_event.get("summary") or "") or self._resolve_title(text, fields)

        if intent == "cancel_event":
            payload = {"event_id": event_id}
        else:  # create_event / update_event
            payload = self._build_event_body(event_summary, fields, caller_event)
            if not payload:
                # Nothing extractable to write - refuse rather than send an
                # empty write (docs/02: fields are never invented).
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        "InferCalendarFieldsNode: no event fields (title/start/"
                        "end/location/description) could be inferred from the request"
                    ],
                }

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_calendar_fields_complete",
            {
                "intent": intent,
                "has_event_id": bool(event_id),
                "has_caller_event": bool(caller_event),
                "n_fields": len(payload),
            },
            state,
        )

        return {
            "event_id": event_id,
            "event_summary": event_summary,
            "calendar_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_event_id(self, text: str, event_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit id only: text mention > id-shaped hint > 'Event ID:' field. Never invented."""
        m = _ID_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = event_hint.strip()
        if hint and _ID_SHAPE_RE.match(hint):
            return hint
        for key, value in fields:
            if key.strip().lower() in _ID_KEYS and _ID_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_title(self, text: str, fields: "list[tuple[str, str]]") -> str:
        m = _TITLE_QUOTED_RE.search(text)
        if m:
            return m.group(1).strip()[:200]
        for key, value in fields:
            if key.strip().lower() in _TITLE_KEYS:
                return value.strip()[:200]
        return ""

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] field lines parsed from the request."""
        fields: "list[tuple[str, str]]" = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields

    def _parse_time(self, value: str) -> "dict[str, str] | None":
        """Explicit ISO-like value -> Google Calendar time object; else None (never invented)."""
        m = _DATETIME_VALUE_RE.match(value.strip())
        if not m:
            return None
        date_part, time_part = m.group(1), m.group(2)
        if time_part:
            hh, mm = time_part.split(":")
            return {"dateTime": f"{date_part}T{hh.zfill(2)}:{mm}:00"}
        return {"date": date_part}  # all-day shape

    def _first_field_time(self, fields: "list[tuple[str, str]]", keys: "tuple[str, ...]") -> "dict[str, str] | None":
        for key, value in fields:
            if key.strip().lower() in keys:
                parsed = self._parse_time(value)
                if parsed:
                    return parsed
        return None

    # -- payload assembly (Google Calendar API v3 Events resource shape) -------

    def _build_event_body(
        self,
        event_summary: str,
        fields: "list[tuple[str, str]]",
        caller_event: "dict[str, Any]",
    ) -> "dict[str, Any]":
        """Caller fields are authoritative; text inference fills only the gaps."""
        body: "dict[str, Any]" = {}
        if event_summary:
            body["summary"] = event_summary
        start = self._parse_time(str(caller_event.get("start") or "")) or self._first_field_time(fields, _START_KEYS)
        end = self._parse_time(str(caller_event.get("end") or "")) or self._first_field_time(fields, _END_KEYS)
        if start:
            body["start"] = start
        if end:
            body["end"] = end
        caller_location = str(caller_event.get("location") or "")
        caller_description = str(caller_event.get("description") or "")
        if caller_location:
            body["location"] = caller_location[:500]
        if caller_description:
            body["description"] = caller_description[:2000]
        for key, value in fields:
            low = key.strip().lower()
            if low in _LOCATION_KEYS and "location" not in body:
                body["location"] = value[:500]
            elif low in _DESC_KEYS and "description" not in body:
                body["description"] = value[:2000]
        return body
