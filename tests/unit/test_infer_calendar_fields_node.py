# CMN-C2-235 - Unit tests: InferCalendarFieldsNode (inner Step 3)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value. Positive payloads are PII-free: the framework
# input mask rewrites Title-Case bigrams in validated_input ("Meeting Room"
# counts!), so quoted titles use a single-word title and location/description
# values stay lower-case. Structured caller data (`caller_event`) arrives over
# the unmasked context bridge and is authoritative over text inference.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_calendar_fields_node import InferCalendarFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_calendar_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "create_event", event_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "event_hint": event_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferCalendarFieldsNode:
    def setup_method(self):
        self.node = InferCalendarFieldsNode()

    def test_create_builds_event_body_from_quoted_title_and_field_lines(self):
        text = (
            'Schedule a meeting titled "Standup"\n'
            "Start: 2026-08-01 14:00\n"
            "End: 2026-08-01 15:00\n"
            "Location: meeting room a\n"
            "Description: weekly cadence"
        )
        result = self.node(_state(text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["event_summary"] == "Standup"
        # State contract: calendar_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["calendar_payload"], str)
        payload = from_json(result["calendar_payload"], {})
        assert payload["summary"] == "Standup"
        assert payload["start"] == {"dateTime": "2026-08-01T14:00:00"}
        assert payload["end"] == {"dateTime": "2026-08-01T15:00:00"}
        assert payload["location"] == "meeting room a"
        assert payload["description"] == "weekly cadence"

    def test_bare_date_becomes_all_day_shape(self):
        text = 'Schedule a workshop titled "Kickoff"\nStart: 2026-08-01'
        result = self.node(_state(text))
        payload = from_json(result["calendar_payload"], {})
        assert payload["start"] == {"date": "2026-08-01"}

    def test_non_iso_datetime_is_never_invented(self):
        text = 'Schedule a meeting titled "Standup"\nStart: tomorrow afternoon'
        result = self.node(_state(text))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["calendar_payload"], {})
        assert "start" not in payload
        assert payload["summary"] == "Standup"

    def test_update_resolves_event_id_from_text(self):
        text = "Reschedule event id evt123 to a later slot\nStart: 2026-08-02 10:00"
        result = self.node(_state(text, intent="update_event"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["event_id"] == "evt123"
        payload = from_json(result["calendar_payload"], {})
        assert payload["start"] == {"dateTime": "2026-08-02T10:00:00"}

    def test_cancel_uses_id_shaped_hint_when_text_has_no_id(self):
        result = self.node(_state("Cancel the weekly sync", intent="cancel_event", event_hint="evt777"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["event_id"] == "evt777"
        assert from_json(result["calendar_payload"], {}) == {"event_id": "evt777"}

    def test_cancel_with_unresolved_id_left_empty_never_invented(self):
        # The unresolved id stays empty here; CallCalendarApiNode surfaces the
        # miss as status=error (never cancel a guessed event).
        result = self.node(_state("Cancel the weekly sync", intent="cancel_event"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["event_id"] == ""
        assert from_json(result["calendar_payload"], {}) == {"event_id": ""}

    def test_non_id_shaped_hint_left_unresolved(self):
        result = self.node(_state("Cancel the weekly sync", intent="cancel_event", event_hint="not a valid id!"))
        assert result["event_id"] == ""

    def test_create_with_nothing_extractable_errors(self):
        result = self.node(_state("please schedule something nice"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("no event fields" in entry for entry in result["error_log"])

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_emits_field_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.infer_calendar_fields_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state('Schedule a meeting titled "Standup"\nStart: 2026-08-01 14:00'))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - shape signals, no content.
        payload = payloads["infer_calendar_fields_complete"]
        assert payload["intent"] == "create_event"
        assert payload["has_event_id"] is False
        assert payload["n_fields"] == 2  # summary + start


class TestCallerEventPrecedence:
    """Validated caller fields (state `caller_event`, via the context bridge)
    are authoritative; text inference fills only the gaps. The request-text
    channel is rewritten by the framework mask, this channel is not - so a
    Title Case caller title must arrive in the payload INTACT."""

    def setup_method(self):
        self.node = InferCalendarFieldsNode()

    def test_caller_summary_wins_over_text_title(self):
        result = self.node(
            _state(
                'Schedule a meeting titled "[MASKED]"\nStart: 2026-08-01 14:00',
                caller_event={"summary": "Quarterly Business Review"},
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["event_summary"] == "Quarterly Business Review"
        payload = from_json(result["calendar_payload"], {})
        assert payload["summary"] == "Quarterly Business Review"
        assert payload["start"] == {"dateTime": "2026-08-01T14:00:00"}  # text fills the gap

    def test_caller_times_and_location_win_over_text_fields(self):
        result = self.node(
            _state(
                "Schedule the review\nStart: 2026-08-01 09:00\nLocation: annex",
                caller_event={
                    "summary": "Design Review",
                    "start": "2026-09-02 10:00",
                    "end": "2026-09-02 11:30",
                    "location": "Osaka Innovation Center",
                    "description": "Bring the v2 mockups.",
                },
            )
        )
        payload = from_json(result["calendar_payload"], {})
        assert payload["start"] == {"dateTime": "2026-09-02T10:00:00"}
        assert payload["end"] == {"dateTime": "2026-09-02T11:30:00"}
        assert payload["location"] == "Osaka Innovation Center"
        assert payload["description"] == "Bring the v2 mockups."

    def test_caller_event_accepted_as_json_string_state_value(self):
        """State carries caller_event as a JSON string (msgpack-safe); the
        node parses it back."""
        from src.schemas.state import to_json

        result = self.node(
            _state(
                "Schedule the review for the team",
                caller_event=to_json({"summary": "Roadmap Session", "start": "2026-10-01"}),
            )
        )
        payload = from_json(result["calendar_payload"], {})
        assert payload["summary"] == "Roadmap Session"
        assert payload["start"] == {"date": "2026-10-01"}  # bare date -> all-day shape

    def test_absent_caller_event_degrades_to_text_inference(self):
        result = self.node(_state('Schedule a meeting titled "Standup"\nStart: 2026-08-01 14:00'))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["event_summary"] == "Standup"
