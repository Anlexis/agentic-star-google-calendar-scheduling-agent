# PB-6: Backbone invoke-order + external-trust boundary (CMN-C2-235).
#
# A real outer-graph invoke exercising the fixed 5-node backbone
# (initialize -> pre_process -> main[inner calendar workflow] -> post_process -> finalize).
#
# The SINGLE external trust gate lives on the outer backbone pre_process
# (required_trust_level = VERIFIED_EXTERNAL). GraphNode.execute() passes the
# caller's InvocationContext into the inner subgraph UNCHANGED (no trust
# elevation), so the inner Google Calendar call (CallCalendarApiNode) is
# ANONYMOUS and runs under the caller's already-gated context. Two trust levels
# are asserted:
#
#   * VERIFIED_EXTERNAL (a real external caller - never for_internal()): passes
#     the pre_process gate, so the full backbone runs IN ORDER and the event
#     evidence surfaces on the structured envelope (status success). The payload
#     is byte-equal to deploy/invoke_payload.json's "input" - this is exactly
#     the deployment first-invoke path.
#   * ANONYMOUS (an under-trusted caller): denied at the pre_process gate before
#     the inner Google Calendar call can run -> status=error, no event evidence.
#
# The Google Calendar call is served by the deterministic NETWORK-FREE v1 stub
# transport (no live tenant, no secret needed - the node runs on the documented
# stub placeholder). Event evidence is asserted by presence (mask-robust: the
# framework/FinalizeNode may mask raw identifiers), never by whole-repr.

import json
import pathlib

import pytest

try:
    from framework.schemas.agent_status import AgentStatus
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel

    from src.graph.graph import GoogleWorkspaceCalendarAgent

    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only when the framework wheel is absent
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(_IMPORT_ERROR is not None, reason=f"framework wheel unavailable: {_IMPORT_ERROR}")

_MAIN_SLOT_NODE = "CalendarWorkflowGraphNode"

# Byte-equal to deploy/invoke_payload.json "input" (asserted below - never retyped drift).
_VALID_PAYLOAD = 'Schedule a meeting titled "deployment check sync".\nStart: 2026-08-01 14:00\nEnd: 2026-08-01 15:00\nLocation: room a'
_SESSION_ID = "deploy-check-001"

_PAYLOAD_PATH = pathlib.Path(__file__).parents[2] / "deploy" / "invoke_payload.json"

_EXPECTED_HISTORY = [
    "InitializeNode",
    "PreProcessNode",
    _MAIN_SLOT_NODE,
    "PostProcessNode",
    "FinalizeNode",
]


def _build_agent():
    agent = GoogleWorkspaceCalendarAgent()
    agent.compile()
    return agent


class TestBackboneInvokeOrder:
    def test_valid_payload_is_byte_equal_to_deploy_payload(self):
        """PB-6 uses the deployment first-invoke request verbatim."""
        deployed = json.loads(_PAYLOAD_PATH.read_text(encoding="utf-8"))
        assert deployed["input"] == _VALID_PAYLOAD
        assert deployed["session_id"] == _SESSION_ID

    def test_verified_external_runs_full_backbone_in_order(self):
        """External (VERIFIED_EXTERNAL) caller: the exact 5-node backbone runs
        in order and event evidence surfaces on the structured envelope."""
        agent = _build_agent()
        result = agent.invoke(
            user_input=_VALID_PAYLOAD,
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-ext", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )
        assert result["status"] == AgentStatus.SUCCESS.value, f"result={result!r}"
        assert result.get("trace_id") and result.get("correlation_id")
        assert result.get("node_history", []) == _EXPECTED_HISTORY
        # Event evidence + confirmation surface as the structured envelope keys
        # (presence, mask-robust - never the raw identifier value).
        assert result.get("event_id") or result.get("event_ref")
        assert result.get("confirmation")
        assert result.get("intent") == "create_event"

    def test_under_trusted_caller_is_denied(self):
        """Under-trusted (ANONYMOUS) caller: ANONYMOUS < VERIFIED_EXTERNAL, so
        the pre_process gate refuses the request before the inner Google
        Calendar call can run - a well-formed error surface (gated, not
        crashed) with no event evidence. Note: the main slot NAME still appears
        in node_history because BaseNode.__call__ short-circuits on the
        already-errored state (its subgraph never executes); post_process is
        skipped by route()."""
        agent = _build_agent()
        result = agent.invoke(
            user_input=_VALID_PAYLOAD,
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-anon", caller_trust_level=TrustLevel.ANONYMOUS),
        )
        assert result["status"] == AgentStatus.ERROR.value, f"result={result!r}"
        assert "PostProcessNode" not in result.get("node_history", [])
        assert not result.get("event_id")
        assert not result.get("event_ref")

    def test_empty_input_surfaces_error_not_crash(self):
        agent = _build_agent()
        result = agent.invoke(
            user_input="   ",
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-ext", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )
        assert result["status"] == AgentStatus.ERROR.value
