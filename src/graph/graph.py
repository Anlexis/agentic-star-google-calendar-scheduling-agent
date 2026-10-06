"""AgentCore Platform v1.0 - CMN-C2-235 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in CalendarWorkflowGraphNode (`main`
slot), which wraps the inner CalendarWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.
"""

from pathlib import Path
from typing import Any, ClassVar

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.graph.base_graph import BaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_event_context
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode, _security_gate_output
from src.schemas.state import State, from_json

# Repo-root runtime config: src/graph/graph.py -> parents[2] = repo root.
# config/agent.yaml is the static registry manifest; runtime parameters
# (max_retry, timeout_s) and the integration sections (google_calendar, llm)
# live in config/config.yaml, which is what the graph reads at run time.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


class CalendarWorkflowGraphNode(GraphNode):
    """Wraps the inner calendar workflow graph; assigned to the `main` slot.

    No constructor arguments (SDK v1 nodes are no-arg) - configuration reaches
    the subgraph via _parent_config(), which loads config/config.yaml.
    """

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> "BaseGraph":
        from src.graph.domain_workflow_graph import CalendarWorkflowGraph

        return CalendarWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # Bridge the VALIDATED caller event data into the inner graph (runs
        # before subgraph.invoke; the inner _extra_initial_state() reads it).
        # The data must not ride only inside the validated_input JSON: the
        # framework's PII mask rewrites that field at node boundaries and real
        # calendar data (Title Case titles, hyphenated numeric event ids)
        # trips the masking heuristics - see context_bridge.py.
        set_caller_event_context(
            {
                "event_hint": str(state.get("event_hint") or ""),
                "event": from_json(state.get("caller_event"), {}) or {},
            }
        )
        # pre_process serialized the request into validated_input (JSON string);
        # structured params travel as JSON and the first inner node parses them back.
        return str(state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: "dict[str, Any]") -> "dict[str, Any]":
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "event_id": sub_result.get("event_id", ""),
            "event_ref": sub_result.get("event_ref", ""),
            "event_summary": sub_result.get("event_summary", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "calendar_payload": sub_result.get("calendar_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> "dict[str, Any]":
        """Forward the runtime config to the inner graph under config["configurable"].

        Loads config/config.yaml and forwards the `google_calendar:`
        integration section and the `llm:` section when declared
        (LLM-synthesis follow-up, docs/02) - never an empty {} (an empty
        _parent_config() would silently make every declared setting dead).
        The runtime scalars (max_retry, timeout_s) are consumed by the outer
        graph itself (the framework run loop reads them from the config passed
        to the graph constructor), so they are not re-forwarded here.
        """
        try:
            runtime = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            # Missing/unreadable config: inner nodes fall back to safe
            # defaults (documented network-free stub client).
            runtime = {}
        configurable: "dict[str, Any]" = {}
        for key in ("google_calendar", "llm"):
            if key in runtime:
                configurable[key] = runtime[key]
        return {"configurable": configurable}


class GoogleWorkspaceCalendarAgent(AgentBaseGraph):
    """CMN-C2-235 outer graph - Google Workspace Calendar Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in CalendarWorkflowGraphNode (`main` slot); Google
    Calendar settings flow from config/config.yaml via _parent_config().
    """

    @property
    def name(self) -> str:
        return "cmn_c2_235"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = CalendarWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        """Structured product envelope (docs/02 "Structured Product Output").

        Extends super().get_output() (output / status / trace_id /
        correlation_id / node_history) with the structured calendar keys.
        Fail-closed posture: the keys are surfaced ONLY on a SUCCESS response
        whose formatted_output re-passes the module-level
        _security_gate_output() scan - on error, a non-dict output, or a gate
        violation the base envelope is returned unchanged.
        """
        output: "dict[str, Any]" = super().get_output(state)
        if state.get("status") != AgentStatus.SUCCESS.value:
            return output
        formatted = state.get("formatted_output")
        if not isinstance(formatted, dict):
            return output
        if _security_gate_output(formatted, is_success=True):
            return output  # fail-closed: never surface unverified evidence
        output["event_id"] = formatted.get("event_id", "")
        output["event_ref"] = formatted.get("event_ref", "")
        output["event_summary"] = formatted.get("event_summary", "")
        output["intent"] = formatted.get("intent", "")
        output["confirmation"] = formatted.get("confirmation", "")
        return output
