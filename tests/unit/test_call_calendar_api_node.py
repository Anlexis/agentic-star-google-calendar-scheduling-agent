# CMN-C2-235 - Unit tests: CallCalendarApiNode (inner Step 4, tool side-effect)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, trust gate unaffected).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's GoogleCalendarClient
# symbol (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_calendar_api_node import CallCalendarApiNode
from src.services.calendar_client import CalendarApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_calendar_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "calendar_payload": to_json({"summary": "Standup", "start": {"dateTime": "2026-08-01T14:00:00"}}),
        "intent": "create_event",
        "event_id": "",
        "event_summary": "Standup",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-calendar-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


# What a real Google Calendar 403 body says back: the API echoes request
# context, so the message can carry an organizer's name and address. Assembled
# at runtime so no contact-shaped literal sits in the tree as a constant.
def _upstream_error_body() -> str:
    return "forbidden by calendar ACL for A. Tanaka <a.tanaka@" + "example.com>"


class _FakeErrorClient:
    """Stands in for GoogleCalendarClient: create raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True
    calendar_id = "primary"

    def create_event(self, payload, api_token):
        raise CalendarApiError(403, _upstream_error_body())


class _FakeTransportFailureClient:
    """Stands in for GoogleCalendarClient: the transport itself blows up."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True
    calendar_id = "primary"

    def create_event(self, payload, api_token):
        raise ConnectionError("https://www.googleapis.com/calendar/v3 refused: " + _upstream_error_body())


class _FakeLiveClient:
    """Stands in for GoogleCalendarClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False
    calendar_id = "primary"

    def create_event(self, payload, api_token):
        _FakeLiveClient.captured = {"payload": payload, "api_token": api_token}
        return {"id": "live-evt-1", "summary": str(payload.get("summary", ""))}


class TestCallCalendarApiNode:
    def setup_method(self):
        self.node = CallCalendarApiNode()

    def test_create_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free v1 stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["event_id"]
        assert result["event_ref"] == f"gcal://calendars/primary/events/{result['event_id']}"
        assert result["event_summary"] == "Standup"

    def test_update_success_echoes_target_event_id(self):
        state = _state(
            intent="update_event",
            event_id="evt123",
            calendar_payload=to_json({"summary": "Standup"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["event_id"] == "evt123"
        assert result["event_ref"] == "gcal://calendars/primary/events/evt123"

    def test_cancel_success_echoes_target_event_id(self):
        state = _state(
            intent="cancel_event",
            event_id="evt999",
            calendar_payload=to_json({"event_id": "evt999"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["event_id"] == "evt999"

    def test_calendar_config_state_field_sets_client_settings(self):
        # The inner graph injects the manifest `google_calendar:` section as the
        # JSON calendar_config state field; the stub transport still serves the call.
        state = _state(calendar_config=to_json({"base_url": "https://calendar.example.test/v3", "calendar_id": "team"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["event_ref"].startswith("gcal://calendars/team/events/")

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"google_calendar": {"calendar_id": "override-cal"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["event_ref"].startswith("gcal://calendars/override-cal/events/")

    def test_missing_payload_errors(self):
        result = self.node(_state(calendar_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_update_with_unresolved_event_id_errors(self):
        state = _state(intent="update_event", event_id="", calendar_payload=to_json({"summary": "Standup"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved event id" in entry for entry in result["error_log"])

    def test_cancel_with_unresolved_event_id_errors(self):
        state = _state(intent="cancel_event", event_id="", calendar_payload=to_json({"event_id": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved event id" in entry for entry in result["error_log"])

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="clone_event"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error_with_the_http_status_only(self, monkeypatch):
        """A Google API error body is remote content - it echoes request
        context, so it can carry an organizer's name and address - and the
        CalendarApiError message embeds it. The log line carries the HTTP
        status and nothing else."""
        monkeypatch.setattr("src.nodes.call_calendar_api_node.GoogleCalendarClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        entry = next(e for e in result["error_log"] if "403" in e)
        assert entry == "CallCalendarApiNode: Google Calendar API error 403"
        assert "Tanaka" not in entry
        assert "forbidden" not in entry
        assert _upstream_error_body() not in "\n".join(result["error_log"])

    def test_transport_failure_surfaces_the_exception_class_only(self, monkeypatch):
        """An arbitrary transport exception string can carry a URL, a host or a
        response fragment; only the class name is a closed-set label."""
        monkeypatch.setattr("src.nodes.call_calendar_api_node.GoogleCalendarClient", _FakeTransportFailureClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        entry = next(e for e in result["error_log"] if "call failed" in e)
        assert entry == "CallCalendarApiNode: Google Calendar call failed (ConnectionError)"
        assert "googleapis.com" not in entry
        assert "Tanaka" not in entry

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing GOOGLE_CALENDAR_TOKEN is a hard
        # error - a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_calendar_api_node.GoogleCalendarClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_calendar_api_node.GoogleCalendarClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"GOOGLE_CALENDAR_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["payload"]["summary"] == "Standup"

    def test_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_calendar_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_calendar_api_complete"]
        assert payload["intent"] == "create_event"
        assert payload["has_event_id"] is True
        assert payload["stub_transport"] is True
