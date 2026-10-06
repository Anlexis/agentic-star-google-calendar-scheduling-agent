"""AgentCore Platform v1.0 - inner workflow Step 5: Confirm.

Formats the created / updated / cancelled Google Calendar event (id +
reference + title) into a human-readable confirmation message, surfacing the
affected event for human review (risk mitigation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VERBS = {
    "create_event": "Created calendar event",
    "update_event": "Updated calendar event",
    "cancel_event": "Cancelled calendar event",
}


class ConfirmNode(FunctionNode):
    """Build the human-readable confirmation."""

    # Inner domain node, read-only formatting of already-produced data -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        event_id = state.get("event_id", "")
        event_ref = state.get("event_ref", "")
        event_summary = state.get("event_summary", "")
        intent = state.get("intent", "")

        if not event_id and not event_ref:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ConfirmNode: no event_id/event_ref to confirm"],
            }

        verb = _VERBS.get(intent, "Processed calendar event")
        parts = [f"{verb} '{event_summary or event_id}'"]
        if event_ref:
            parts.append(f"ref={event_ref}")
        if event_id:
            parts.append(f"id={event_id}")
        confirmation = " - ".join(parts)

        # Audit the confirmed action - intent + reference presence (no content).
        emit_trace_event(
            "confirm_complete",
            {"intent": intent, "has_event_ref": bool(event_ref)},
            state,
        )

        return {
            "confirmation": confirmation,
            "result": {
                "event_id": event_id,
                "event_ref": event_ref,
                "confirmation": confirmation,
            },
            "status": AgentStatus.SUCCESS.value,
        }
