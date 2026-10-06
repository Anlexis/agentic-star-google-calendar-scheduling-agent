"""Regression: an ERROR envelope must not disclose calendar-write evidence.

Molt source review, 2026-09-03: the `errored` branch of PostProcessNode cleared
`result` and every other output-bearing field, then re-populated `formatted_output`
with `event_id` / `event_ref` read straight out of state. Those identifiers ARE the
calendar-action evidence — `_security_gate_output` blocks a SUCCESS that lacks them —
so shipping them inside an error envelope discloses that a calendar write occurred
and which record it touched.

The containment contract is not "clear `result`". It is "no un-gated caller-facing
content survives on any error path", and that includes whatever the replacement
`formatted_output` still carries.
"""
from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import PostProcessNode

_EVENT_ID = "evt_abc123456789"
_EVENT_REF = "https://calendar.example.test/e/abc123456789"


def _errored_state() -> dict:
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": ["upstream calendar API rejected the write"],
        "event_id": _EVENT_ID,
        "event_ref": _EVENT_REF,
        "event_summary": "Quarterly board review with external counsel",
        "confirmation": "Booked for 2026-09-10 09:00 JST",
        "calendar_payload": '{"attendees": ["ceo@example.test"]}',
        "result": "Your meeting has been booked.",
    }


def _flatten(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


def test_error_envelope_carries_no_event_evidence():
    out = PostProcessNode().execute(_errored_state())

    assert out["status"] == AgentStatus.ERROR.value

    # formatted_output must be PRESENT and TRUTHY: the framework projects
    # `formatted_output or result`, so a falsy value re-opens the fallback onto
    # whatever survived in state. Presence AND non-emptiness, not just presence.
    assert "formatted_output" in out
    assert out["formatted_output"]

    shipped = _flatten(out["formatted_output"])
    assert _EVENT_ID not in shipped
    assert _EVENT_REF not in shipped
    assert "abc123456789" not in shipped
    assert "Quarterly board review" not in shipped
    assert "Booked for" not in shipped
    assert "ceo@example.test" not in shipped


def test_error_envelope_clears_result_channels():
    out = PostProcessNode().execute(_errored_state())
    for field in ("result", "event_summary", "confirmation", "calendar_payload"):
        assert field in out, f"{field} not cleared on the error path"
        assert not out[field], f"{field} still carries content on the error path"


def test_success_path_still_returns_the_answer():
    """Control: containment must not break the clean path."""
    out = PostProcessNode().execute({
        "status": AgentStatus.SUCCESS.value,
        "event_id": _EVENT_ID,
        "event_ref": _EVENT_REF,
        "event_summary": "Quarterly board review",
        "intent": "create",
        "confirmation": "Booked",
        "calendar_payload": "{}",
    })
    assert out["status"] == AgentStatus.SUCCESS.value
    assert _EVENT_ID in _flatten(out["formatted_output"])
