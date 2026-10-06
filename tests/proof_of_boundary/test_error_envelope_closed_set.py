# PB (closed-set error contract): what the CALLER receives when the agent
# fails, driven through the real ASGI /invoke entry point.
#
# Clearing the answer and bounding the error channel are two different
# properties, and passing the first says nothing about the second. The envelope
# resolves its payload as `formatted_output or result` with no status check, so
# an error return has to be truthy AND record-free AND free of node-authored
# text. `error_log` is where that text lives: node-authored lines, and wherever
# a node touches the Google Calendar API, whatever the API's own error body
# said. Truncating it, stripping paths from it or redacting credentials out of
# it is not a closed set - identifiers, titles, attendee names and arbitrary
# third-party response bodies all survive those.
#
# So the property asserted here is: on every non-success path the invoke body
# carries values this module's own code chose from a declared set, and a
# sentinel seeded into error_log appears NOWHERE in it - not as a value, not as
# a key, at any depth.
#
# Two fault injections, both on the DATA path and never on the gate under test:
#
#   1. the inner->outer merge loses the event-evidence keys while keeping the
#      answer in `result`, and carries an upstream failure line in error_log -
#      exactly the contract drift the output gate exists to catch. This is the
#      REACHABLE non-success path on this backbone: the inner graph succeeded,
#      so `main` returns SUCCESS, route() sends the run to post_process, and
#      the gate refuses there.
#   2. the Google Calendar client raises the documented API error with a
#      response body carrying the sentinel. AgentBaseGraph.route() sends an
#      ERROR status straight to `finalize`, so post_process never runs on this
#      path and the invoke body is produced by get_output() alone - which is
#      why it is asserted separately: it is the channel a get_output() override
#      would leak through, and it is where the source-half fix is visible.
#
# The clean-path control is deliberate: without it every containment assertion
# below would also pass on an agent that produced nothing at all.

import json
import os
import warnings

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import ERROR_REASONS, _REASON_OUTPUT_WITHHELD
from src.services.calendar_client import CalendarApiError

_TOKEN = "pb-closed-set-test-token"

_REQUEST = (
    'Schedule a meeting titled "closed set probe sync"\n'
    "Start: 2026-09-05 09:00\n"
    "End: 2026-09-05 10:00\n"
    "Location: room c"
)


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed Google API response body. The
    token is assembled at runtime so no credential-shaped literal is committed."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: Google said {'organizer':'A. Tanaka','token':'" + token + "'}"


# Fragments of the sentinel that must appear nowhere in the invoke body.
_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: Google said", "sk-" + "live-")


def _leaves(value):
    """Every key and scalar inside `value`, rendered as text, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _leaves(item)
    else:
        yield str(value)


def _assert_sentinel_absent(body):
    leaves = list(_leaves(body))
    for fragment in _SENTINEL_FRAGMENTS:
        assert not any(fragment in leaf for leaf in leaves), (fragment, body)
    rendered = json.dumps(body, default=str)
    assert not any(fragment in rendered for fragment in _SENTINEL_FRAGMENTS), rendered


@pytest.fixture(scope="module")
def client():
    os.environ["INVOKE_AUTH_TOKEN"] = _TOKEN
    with warnings.catch_warnings():
        # Import-time noise from the sync test client shim, not app behaviour.
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

        import src.api.server as server

        with TestClient(server.app) as test_client:
            yield test_client


def _invoke(client, payload):
    return client.post("/invoke", json=payload, headers={"Authorization": f"Bearer {_TOKEN}"})


def _drift_and_seed_error_log(monkeypatch):
    """Data-path fault: the inner->outer merge loses the evidence keys (so the
    output gate refuses) and carries an upstream failure line in error_log."""
    from src.graph.graph import CalendarWorkflowGraphNode

    original = CalendarWorkflowGraphNode.merge_output

    def _drifted(self, state, sub_result):
        merged = original(self, state, sub_result)
        merged["event_id"] = ""
        merged["event_ref"] = ""
        merged["error_log"] = list(merged.get("error_log") or []) + [_sentinel()]
        return merged

    monkeypatch.setattr(CalendarWorkflowGraphNode, "merge_output", _drifted)


class TestRefusedResponseCarriesClosedSetLabelsOnly:
    """The reachable non-success path: the output gate refuses at post_process."""

    def test_clean_path_control_still_delivers_the_answer(self, client):
        """CONTROL: without the drift this same request succeeds and the answer
        IS delivered, so the assertions below are about containment rather than
        about a request that never produced anything."""
        body = _invoke(client, {"input": _REQUEST, "session_id": "pb-cs-ctl"}).json()

        assert body["status"] == AgentStatus.SUCCESS.value
        assert body["output"].get("confirmation")
        assert "closed set probe sync" in json.dumps(body)
        assert "reason" not in body["output"]

    def test_envelope_is_a_declared_reason_code_and_nothing_else(self, client, monkeypatch):
        _drift_and_seed_error_log(monkeypatch)
        body = _invoke(client, {"input": _REQUEST, "session_id": "pb-cs-1"}).json()

        assert body["status"] == AgentStatus.ERROR.value
        assert set(body["output"]) == {"reason"}, body["output"]
        assert set(body["output"].values()) <= ERROR_REASONS, body["output"]
        assert body["output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_envelope_is_truthy_so_the_result_fallback_never_fires(self, client, monkeypatch):
        """`formatted_output or result`: a falsy payload re-selects the un-gated
        answer, and the failure mode looks identical to no fix at all."""
        _drift_and_seed_error_log(monkeypatch)
        body = _invoke(client, {"input": _REQUEST, "session_id": "pb-cs-2"}).json()

        assert body["output"], "a falsy payload re-opens the `result` fallback"
        assert "closed set probe sync" not in json.dumps(body)
        assert "Created calendar event" not in json.dumps(body)
        assert "gcal://" not in json.dumps(body)

    def test_seeded_error_text_appears_nowhere_in_the_invoke_body(self, client, monkeypatch):
        _drift_and_seed_error_log(monkeypatch)
        body = _invoke(client, {"input": _REQUEST, "session_id": "pb-cs-3"}).json()

        assert body["status"] == AgentStatus.ERROR.value
        _assert_sentinel_absent(body)

    def test_no_error_log_key_and_no_gate_wording_in_the_body(self, client, monkeypatch):
        """The violation entries are the internal channel's business. Neither
        the key nor the gate's own wording is part of the caller contract."""
        _drift_and_seed_error_log(monkeypatch)
        body = _invoke(client, {"input": _REQUEST, "session_id": "pb-cs-4"}).json()

        assert "error_log" not in body
        assert "output gate" not in json.dumps(body)
        assert "Traceback" not in json.dumps(body)


class TestUpstreamApiErrorNeverReachesTheCaller:
    """The other non-success path: the Google Calendar call fails.

    route() sends an ERROR status straight to finalize, so post_process does not
    run and the invoke body comes from get_output() alone. Two things are
    asserted: the body carries no error text (the channel a get_output()
    override would leak through), and the API's own response body never entered
    error_log in the first place (the source half - the node reduces the failure
    to its HTTP status).
    """

    @staticmethod
    def _fail_the_calendar_call(monkeypatch):
        from src.services.calendar_client import GoogleCalendarClient

        def _raise(self, payload, api_token):
            raise CalendarApiError(403, _sentinel())

        monkeypatch.setattr(GoogleCalendarClient, "create_event", _raise)

    def test_upstream_error_body_appears_nowhere_in_the_invoke_body(self, client, monkeypatch):
        self._fail_the_calendar_call(monkeypatch)
        body = _invoke(client, {"input": _REQUEST, "session_id": "pb-cs-5"}).json()

        assert body["status"] == AgentStatus.ERROR.value
        _assert_sentinel_absent(body)
        assert "error_log" not in body
        assert "Traceback" not in json.dumps(body)

    def test_no_event_evidence_is_surfaced_on_a_failed_write(self, client, monkeypatch):
        """The structured product keys are SUCCESS-only; a failed calendar write
        must not tell the caller that a record was nonetheless touched."""
        self._fail_the_calendar_call(monkeypatch)
        body = _invoke(client, {"input": _REQUEST, "session_id": "pb-cs-6"}).json()

        for key in ("event_id", "event_ref", "event_summary", "confirmation"):
            assert key not in body, f"{key} surfaced on an error envelope"
        assert not body["output"]

    def test_the_api_response_body_never_entered_error_log(self, client, monkeypatch):
        """The source half, at the node that owns the call: the HTTP status is
        the closed-set part of the failure and is all that is written."""
        from src.nodes.call_calendar_api_node import CallCalendarApiNode
        from src.schemas.state import to_json

        self._fail_the_calendar_call(monkeypatch)
        result = CallCalendarApiNode().execute(
            {
                "calendar_payload": to_json({"summary": "Standup"}),
                "intent": "create_event",
                "correlation_id": "pb-cs-source",
                "session_id": "pb-cs-source",
                "thread_id": "pb-cs-source",
                "trace_id": "pb-cs-source",
                "error_log": [],
            }
        )

        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"] == ["CallCalendarApiNode: Google Calendar API error 403"]
        _assert_sentinel_absent(result)
