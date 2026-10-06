# CMN-C2-235 - Unit tests: ConfirmNode (inner Step 5)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "event_id": "evt123",
        "event_ref": "gcal://calendars/primary/events/evt123",
        "event_summary": "Standup",
        "intent": "create_event",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_create_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Created calendar event" in result["confirmation"]
        assert "'Standup'" in result["confirmation"]
        assert "ref=gcal://calendars/primary/events/evt123" in result["confirmation"]
        assert "id=evt123" in result["confirmation"]
        assert result["result"]["event_id"] == "evt123"
        assert result["result"]["event_ref"] == "gcal://calendars/primary/events/evt123"

    def test_update_verb(self):
        result = self.node(_state(intent="update_event"))
        assert "Updated calendar event" in result["confirmation"]

    def test_cancel_verb(self):
        result = self.node(_state(intent="cancel_event"))
        assert "Cancelled calendar event" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed calendar event" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(event_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=evt123" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_event_id_when_summary_missing(self):
        result = self.node(_state(event_summary=""))
        assert "'evt123'" in result["confirmation"]

    def test_missing_event_evidence_errors(self):
        result = self.node(_state(event_id="", event_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_emits_reference_presence_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.confirm_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        assert payloads["confirm_complete"] == {"intent": "create_event", "has_event_ref": True}
