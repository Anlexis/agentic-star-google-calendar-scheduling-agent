"""AgentCore Platform v1.0 - inner workflow Step 4: CallCalendarApi (tool side-effect).

Performs the create/update/cancel call via src/services/calendar_client.py.
v1 executes against the client's deterministic NETWORK-FREE stub transport
(Google Calendar API v3 request/response shapes; documented limitation,
docs/02 "v1 Limitation - Google Calendar client") - no request leaves the
process and v1 does not write to a real Google tenant. A LIVE transport is a
construction-time injection point on GoogleCalendarClient reserved for
deployment integration; the deployed v1 graph never injects one.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller at the trust
       gate before the call ever runs (a deployment first-invoke failure). The
       node therefore stays ANONYMOUS.
  Secrets: the integration token is read via ctx.secrets.get("GOOGLE_CALENDAR_TOKEN")
       (InvocationContext.from_state(state)) - never os.environ, never stored in
       state. v1 note: while the deterministic NETWORK-FREE stub transport is
       active a missing token is tolerated (a sentinel placeholder is used - it
       is never sent anywhere because no request leaves the process); with a
       LIVE transport injected, a missing token is a hard status=error - a real
       API is never called unauthenticated.
  Audit: emit_trace_event() is called on the success path - a side-effect against
       an external calendar system; HTTP 4xx/5xx surfaces as status=error +
       error_log (no silent pass). The log line carries the HTTP status only -
       never the Google API's own message, which is remote content - and a
       transport failure is reported by exception class, never by its text.

Configuration: this node takes NO constructor arguments (SDK v1 nodes are
no-arg). Google Calendar settings (base_url, calendar_id) arrive as the JSON
`calendar_config` state field - injected by the inner graph's
_extra_initial_state() from the manifest section forwarded by
CalendarWorkflowGraphNode._parent_config() - or via the optional
`config["configurable"]["google_calendar"]` argument for direct invocation.
The client is constructed locally per call (no module-global mutation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.calendar_client import CalendarApiError, GoogleCalendarClient

_SECRET_KEY = "GOOGLE_CALENDAR_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"


class CallCalendarApiNode(FunctionNode):
    """Create / update / cancel a calendar event (v1: network-free stub transport)."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]", config: "dict[str, Any] | None" = None) -> "dict[str, Any]":
        payload = from_json(state.get("calendar_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallCalendarApiNode: missing calendar_payload"],
            }

        intent = state.get("intent", "") or ""

        # Settings: manifest section from state (graph-injected), overridable via
        # an explicit config["configurable"]["google_calendar"] for direct
        # invocation. Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("calendar_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("google_calendar") or {}
        settings.update(override)

        # Client built locally per call from configuration only (base_url /
        # calendar_id). v1 deliberately injects NO transports, so every call
        # resolves to the deterministic NETWORK-FREE stub (documented
        # limitation, docs/02 "v1 Limitation - Google Calendar client"). A
        # live transport is injected HERE, at construction time, when a
        # deployment integration supplies one.
        kwargs: "dict[str, Any]" = {}
        base_url = str(settings.get("base_url", "") or "").strip()
        calendar_id = str(settings.get("calendar_id", "") or "").strip()
        if base_url:
            kwargs["base_url"] = base_url
        if calendar_id:
            kwargs["calendar_id"] = calendar_id
        client = GoogleCalendarClient(**kwargs)

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # v1 stub limitation: no request leaves the process, so run with
                # a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallCalendarApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        event_id = state.get("event_id", "") or str(payload.get("event_id", "") or "")
        event_summary = state.get("event_summary", "") or ""

        try:
            if intent == "create_event":
                resp = client.create_event(payload, api_token) or {}
                result_id = str(resp.get("id", ""))
                event_summary = event_summary or str(resp.get("summary", ""))
            elif intent == "update_event":
                if not event_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallCalendarApiNode: unresolved event id - cannot update event"],
                    }
                resp = client.update_event(event_id, payload, api_token) or {}
                result_id = str(resp.get("id", "")) or event_id
                event_summary = event_summary or str(resp.get("summary", ""))
            elif intent == "cancel_event":
                if not event_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallCalendarApiNode: unresolved event id - cannot cancel event"],
                    }
                resp = client.cancel_event(event_id, api_token) or {}
                result_id = str(resp.get("id", "")) or event_id
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"CallCalendarApiNode: unknown intent '{intent}'"],
                }
        except CalendarApiError as exc:
            # The HTTP status is the closed-set part of the failure and is all
            # that is logged. CalendarApiError's own message embeds the Google
            # API error body (_err_message() reads error.message, falling back
            # to str(body)) - remote content that can carry event titles,
            # attendee identifiers or echoed request fields - and capping or
            # stripping it would still leave arbitrary text in the log line, so
            # it is not echoed at all. The status is bound to a local first so
            # no caught exception appears in an f-string.
            api_status = exc.status_code
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallCalendarApiNode: Google Calendar API error {api_status}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Class name only: an arbitrary exception string can carry a URL, a
            # host, a header or a response fragment. Bound to a local first for
            # the same reason as above.
            failure_class = type(exc).__name__
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallCalendarApiNode: Google Calendar call failed ({failure_class})"],
            }

        event_ref = f"gcal://calendars/{client.calendar_id}/events/{result_id}" if result_id else ""

        # Audit the tool side-effect - intent + presence signals only,
        # never event content or credentials.
        emit_trace_event(
            "call_calendar_api_complete",
            {
                "intent": intent,
                "has_event_id": bool(result_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "event_id": result_id or event_id,
            "event_ref": event_ref,
            "event_summary": event_summary,
            "status": AgentStatus.SUCCESS.value,
        }
