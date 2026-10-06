# CMN-C2-235 - Unit tests: ClassifyIntentNode (inner Step 2)
# Intents: create_event / update_event / cancel_event (deterministic keyword
# heuristic, v1 - no LLM). Every intent in this WRITE agent mutates the tenant
# calendar, so - unlike the 273 read/write agent with its read-only fallback -
# a no-signal request is a HARD status=error (a guessed write is never
# acceptable; docs/02 Design Decision Record).
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_create_event(self):
        result = self.node(_state("Schedule a planning meeting for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "create_event"

    def test_keyword_update_event(self):
        result = self.node(_state("Reschedule the weekly sync to a later slot"))
        # update is checked before create, so "reschedule" never matches
        # create's "schedule" first.
        assert result["intent"] == "update_event"

    def test_keyword_cancel_event(self):
        result = self.node(_state("Cancel the weekly sync for the team"))
        assert result["intent"] == "cancel_event"

    def test_cancel_wins_over_other_signals(self):
        # cancel is the most explicit destructive signal and is checked first.
        result = self.node(_state("Cancel the meeting instead of rescheduling it"))
        assert result["intent"] == "cancel_event"

    def test_no_signal_is_hard_error_never_a_guessed_write(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("could not determine" in entry for entry in result["error_log"])
        assert "intent" not in result

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Schedule a planning meeting for the team"))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"] == {"intent": "create_event"}
