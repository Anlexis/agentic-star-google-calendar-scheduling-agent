# CMN-C2-235 - Unit tests: inner CalendarWorkflowGraph (BaseGraph) contract.
# The compiled outer path is
# exercised end-to-end by tests/proof_of_boundary/test_pb_invoke_order.py; this
# module unit-checks the inner graph's identity, config forwarding, routing,
# output contract, and a direct inner invoke on the network-free v1 stub.

from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import set_caller_event_context
from src.graph.domain_workflow_graph import CalendarWorkflowGraph
from src.schemas.state import State, from_json


def _graph(config=None):
    return CalendarWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "google_workspace_calendar_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_calendar_config_as_json():
    set_caller_event_context({})
    g = _graph({"configurable": {"google_calendar": {"base_url": "https://calendar.example.test/v3"}}})
    extra = g._extra_initial_state()
    # State contract: forwarded as a JSON string, not a native dict.
    assert isinstance(extra["calendar_config"], str)
    assert from_json(extra["calendar_config"], {}) == {"base_url": "https://calendar.example.test/v3"}


def test_extra_initial_state_empty_without_google_calendar_section():
    set_caller_event_context({})  # deterministic: clear any caller data stashed by other tests
    assert _graph()._extra_initial_state() == {}


def test_extra_initial_state_seeds_caller_event_from_bridge():
    """The validated caller event data crosses the graph boundary on the
    context bridge and is seeded into the inner initial state (unmasked
    channel - see context_bridge.py)."""
    set_caller_event_context({"event_hint": "evt12345", "event": {"summary": "Quarterly Business Review"}})
    try:
        extra = _graph()._extra_initial_state()
        assert extra["event_hint"] == "evt12345"
        assert from_json(extra["caller_event"], {}) == {"summary": "Quarterly Business Review"}
    finally:
        set_caller_event_context({})


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_event_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {
                "event_id": "evt123",
                "event_ref": "gcal://calendars/primary/events/evt123",
                "confirmation": "ok",
            },
            "status": AgentStatus.SUCCESS.value,
            "intent": "create_event",
            "event_id": "evt123",
            "event_ref": "gcal://calendars/primary/events/evt123",
            "event_summary": "Standup",
            "confirmation": "ok",
            "calendar_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "create_event"
    assert out["event_ref"] == "gcal://calendars/primary/events/evt123"
    assert out["confirmation"] == "ok"
    assert out["output"] == {
        "event_id": "evt123",
        "event_ref": "gcal://calendars/primary/events/evt123",
        "confirmation": "ok",
    }


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_create_on_v1_stub():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node is
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"google_calendar": {"base_url": "https://www.googleapis.com/calendar/v3"}}})
    g.compile()
    result = g.invoke(user_input='Schedule a meeting titled "Standup"\nStart: 2026-08-01 14:00\nEnd: 2026-08-01 15:00')
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["event_id"]
    assert result["event_ref"].startswith("gcal://calendars/")
    assert result["intent"] == "create_event"
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferCalendarFieldsNode",
        "CallCalendarApiNode",
        "ConfirmNode",
    ]
