"""AgentCore Platform v1.0 - inner calendar workflow graph (Cat 2 domain workflow).

Instantiated by CalendarWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_calendar_fields
          -> call_calendar_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    google_calendar - config/config.yaml integration section (base_url,
                      calendar_id, ...); injected into State as the JSON
                      `calendar_config` field via _extra_initial_state() so
                      the no-arg nodes can read it
    llm             - reserved for the documented LLM-synthesis follow-up
                      (docs/02 "v1 Implementation Note"); unused in deterministic v1

The validated caller event data (event-id hint + structured event fields)
arrives over the context bridge (src/graph/context_bridge.py) and is seeded
into the inner initial state by _extra_initial_state() - the graph boundary
does not forward outer state fields on its own.

Nodes are registered WITHOUT constructor arguments (SDK v1 nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_calendar_fields_node import InferCalendarFieldsNode
from src.nodes.call_calendar_api_node import CallCalendarApiNode
from src.nodes.confirm_node import ConfirmNode
from src.graph.context_bridge import get_caller_event_context
from src.schemas.state import State, to_json


class CalendarWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "google_workspace_calendar_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the google_calendar section is optional (the
        # client falls back to the documented default base_url/calendar_id +
        # the network-free v1 stub transport), and a missing/unusable setting
        # is handled at CallCalendarApiNode.execute() as a graceful
        # status=error rather than a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_calendar_fields"] = InferCalendarFieldsNode()
        self._nodes["call_calendar_api"] = CallCalendarApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_calendar_fields")
        self._sg.add_edge("infer_calendar_fields", "call_calendar_api")
        self._sg.add_edge("call_calendar_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: AgentState) -> str:
        # Required by BaseGraph ABC. Linear topology -> never called unless an
        # add_conditional_edges() references it.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> "dict[str, Any]":
        # Two seeds, both msgpack-safe JSON strings:
        # - the config/config.yaml `google_calendar` section (arriving under
        #   config["configurable"] from _parent_config()) as `calendar_config`,
        #   so the no-arg CallCalendarApiNode can read it;
        # - the validated caller event data (event-id hint + structured event
        #   fields) from the context bridge, so it reaches the inner nodes
        #   UNMASKED - the serialized-request copy is rewritten by the
        #   framework's masking heuristics (see context_bridge.py).
        extra: "dict[str, Any]" = {}
        configurable = self.config.get("configurable") or {}
        google_calendar = configurable.get("google_calendar") or {}
        if google_calendar:
            extra["calendar_config"] = to_json(google_calendar)
        caller = get_caller_event_context()
        if caller.get("event_hint"):
            extra["event_hint"] = str(caller["event_hint"])
        if caller.get("event"):
            extra["caller_event"] = to_json(caller["event"])
        return extra

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "event_id": state.get("event_id", ""),
            "event_ref": state.get("event_ref", ""),
            "event_summary": state.get("event_summary", ""),
            "confirmation": state.get("confirmation", ""),
            "calendar_payload": state.get("calendar_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
