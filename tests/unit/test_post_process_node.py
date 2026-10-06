# CMN-C2-235 - Unit tests: PostProcessNode (outer backbone, domain output gate).
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); this backbone formatter declares ANONYMOUS -> the
# state builder sets caller_trust_level = TrustLevel.ANONYMOUS.value. The
# domain output gate is the MODULE-LEVEL _security_gate_output() helper (the
# framework gate methods are @final and the real SDK auto-wraps _extra_ hooks),
# so the helper is also unit-tested directly as a plain function - including
# the NESTED walk: the caller-facing output embeds the assembled event body as
# a nested mapping, and a credential-shaped string riding one level down must
# be caught exactly like a top-level one (probed BOTH ways: the nested leak
# case AND a top-level control that proves the scanner itself works). Mapping
# KEYS are scanned too - calendar_payload is parsed back from JSON, so a key is
# as publishable as a value.
#
# Containment: refusing is not containing, and clearing the answer is not the
# same property as bounding the error channel. The framework projects
# `formatted_output or result` with no status check, and the outer merge step
# has already written the inner workflow answer to `result` - so an error
# return that leaves state untouched still ships that answer inside the ERROR
# envelope, and one that replaces it with error_log still publishes
# node-authored text. Every non-success return is therefore asserted to clear
# the output-bearing fields AND to install a TRUTHY envelope made only of this
# module's declared reason codes (a falsy envelope re-selects `result`, which
# is half the defect; a node-authored one is the other half).
#
# The errored-state branch is driven through execute() DIRECTLY where noted:
# AgentBaseGraph.route() sends an ERROR status straight to `finalize` and
# BaseNode.__call__ short-circuits on an already-errored state, so that branch
# is only reachable from inside - and it must still be a closed set.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import (
    ERROR_REASONS,
    _OUTPUT_BEARING_FIELDS,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    _UNNAMEABLE_KEY,
    PostProcessNode,
    _safe_path,
    _security_gate_output,
)
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "event_id": "evt123",
        "event_ref": "gcal://calendars/primary/events/evt123",
        "event_summary": "Standup",
        "intent": "create_event",
        "confirmation": "Created calendar event 'Standup' - ref=gcal://calendars/primary/events/evt123 - id=evt123",
        "calendar_payload": to_json({"summary": "Standup"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _bearer_like(fill: str = "a") -> str:
    # Built at runtime so no credential-shaped literal is committed.
    return "Bearer " + fill * 24


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed Google API response body. The
    token is assembled at runtime so no credential-shaped literal is committed."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: Google said {'organizer':'A. Tanaka','token':'" + token + "'}"


# Fragments of the sentinel that must survive nowhere in a returned mapping.
_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: Google said", "sk-" + "live-")


def _leaves(value):
    """Every key and scalar inside `value`, rendered as text, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _leaves(item)
    else:
        yield str(value)


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: AgentStatus is a str-Enum, so == passes for the
        # bare enum too - only an exact type check catches a bare-enum write.
        assert type(result["status"]) is str  # noqa: E721 - exact type IS the assertion
        out = result["formatted_output"]
        assert out["event_id"] == "evt123"
        assert out["event_ref"] == "gcal://calendars/primary/events/evt123"
        assert out["intent"] == "create_event"
        assert out["confirmation"].startswith("Created calendar event")
        # JSON round-trip: the calendar_payload string surfaces parsed.
        assert out["calendar_payload"] == {"summary": "Standup"}
        # A clean response carries no reason code.
        assert "reason" not in out

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            event_id="",
            event_ref="",
            error_log=["CallCalendarApiNode: Google Calendar API error 403"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "Google Calendar API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_direct_execute_error_branch_publishes_the_reason_code_only(self):
        """Defence in depth for direct invocation (no framework wrapper): the
        error branch publishes a declared reason code and nothing else -
        upstream error text is not redacted and forwarded, it is not forwarded."""
        result = self.node.execute(
            _state(
                status=AgentStatus.ERROR.value,
                event_id="",
                event_ref="",
                error_log=[f"upstream said: {_bearer_like()}"],
            )
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert _bearer_like() not in json.dumps(result, default=str)
        # The same containment omission applies on this branch: the inner
        # answer merged into `result` must not survive the error return.
        for field in _OUTPUT_BEARING_FIELDS:
            assert result[field] == "", f"{field} not cleared on the error branch"
        # Truthy by construction (a non-empty mapping), so the framework's
        # `formatted_output or result` projection stops here.
        assert result["formatted_output"]

    def test_gate_blocks_success_without_event_evidence(self):
        """Full node path: a SUCCESS output missing event_id/event_ref is
        blocked AND contained - the pre-gate answer sitting in `result` is
        cleared, not merely accompanied by an error status."""
        result = self.node(_state(event_id="", event_ref="", result={"confirmation": "Created calendar event"}))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in result["error_log"])
        for field in _OUTPUT_BEARING_FIELDS:
            assert result[field] == "", f"{field} not cleared on a gate violation"
        assert "Created calendar event" not in str(result)

    def test_gate_blocks_credential_nested_in_payload_full_node_path(self):
        """The nested leak case through the real node call: a credential-shaped
        string inside the assembled event body (one level down) is blocked."""
        result = self.node(_state(calendar_payload=to_json({"summary": "Standup", "description": _bearer_like()})))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("calendar_payload.description" in entry for entry in result["error_log"])
        assert _bearer_like() not in "\n".join(result["error_log"])  # path named, value never echoed
        # Contained: the offending output is discarded, not returned alongside
        # the error, and the framework projection lands on the truthy envelope.
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        for field in _OUTPUT_BEARING_FIELDS:
            assert result[field] == ""


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_event_evidence(self):
        violations = _security_gate_output(
            {"event_id": "evt123", "event_ref": "gcal://calendars/primary/events/evt123", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_event_evidence(self):
        violations = _security_gate_output(
            {"event_id": "", "event_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "event_id/event_ref" in violations[0]

    def test_blocks_credential_shaped_value_top_level_control(self):
        """Top-level control: proves the scanner itself works, so the nested
        probes below distinguish 'gate is blind' from 'probe is wrong'."""
        violations = _security_gate_output(
            {"event_id": "evt123", "note": _bearer_like()},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    @pytest.mark.parametrize(
        "formatted,leak_path",
        [
            (
                {"event_id": "evt123", "calendar_payload": {"description": _bearer_like()}},
                "calendar_payload.description",
            ),
            ({"event_id": "evt123", "calendar_payload": {"a": {"b": "sk-" + "z" * 24}}}, "calendar_payload.a.b"),
            ({"event_id": "evt123", "items": [{"note": "eyJ" + "h" * 16 + ".x.y"}]}, "items[0].note"),
        ],
    )
    def test_blocks_credential_nested_at_any_depth(self, formatted, leak_path):
        violations = _security_gate_output(formatted, is_success=True)
        assert any(leak_path in v for v in violations), violations

    def test_blocks_credential_shaped_mapping_key(self):
        """A key is as publishable as a value: calendar_payload is parsed back
        from JSON, so a credential riding a KEY would ship with the answer."""
        jwt_like = "eyJ" + "c" * 40
        violations = _security_gate_output(
            {"event_id": "evt123", "calendar_payload": {jwt_like: "x"}},
            is_success=True,
        )
        assert violations

    def test_credential_shaped_key_is_withheld_from_the_label(self):
        """The label is built from mapping keys, so a credential-shaped key
        would otherwise be quoted by the very violation that reports it - and
        that label travels in error_log, where the framework's credential scan
        raises and discards the cleared result. The key is withheld instead."""
        jwt_like = "eyJ" + "c" * 40
        violations = _security_gate_output(
            {"event_id": "evt123", "calendar_payload": {jwt_like: "x"}},
            is_success=True,
        )
        assert all(jwt_like not in v for v in violations)
        assert any(_UNNAMEABLE_KEY in v for v in violations)

    def test_withheld_marker_survives_path_normalisation(self):
        """The marker must be spelled in the inert alphabet: _safe_path()
        rewrites everything else, so an angle-bracketed marker would arrive in
        the log as a different string and stop being recognisable."""
        assert _safe_path(_UNNAMEABLE_KEY) == _UNNAMEABLE_KEY

    def test_clean_nested_structure_passes(self):
        violations = _security_gate_output(
            {
                "event_id": "evt123",
                "calendar_payload": {
                    "summary": "Standup",
                    "start": {"dateTime": "2026-08-01T14:00:00"},
                    "attendees_note": "room booked",
                },
            },
            is_success=True,
        )
        assert violations == []

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"event_id": "", "event_ref": ""}, is_success=False)
        assert violations == []


class TestErrorReturnContainment:
    """Every error return clears the output-bearing state fields.

    The framework's final projection is `formatted_output or result` with no
    status check, so the ONLY thing standing between a refused response and the
    un-gated inner answer is this clearing. Each property below is pinned
    directly rather than inferred from the end-to-end result, so a regression
    names its own cause.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _leaky_state(self, **overrides):
        """A state carrying a fully-formed inner answer in `result`."""
        base = _state(
            result={
                "event_id": "evt123",
                "event_ref": "gcal://calendars/primary/events/evt123",
                "confirmation": "Created calendar event 'Standup'",
            },
            redaction_flags=to_json(["phone"]),
        )
        base.update(overrides)
        return base

    def test_declared_reason_codes_are_truthy_envelopes(self):
        """The envelope must be TRUTHY. `""` / `{}` / `[]` are falsy, and the
        projection would fall through to `result` - re-opening the defect while
        every status assertion still passed."""
        for reason in ERROR_REASONS:
            assert {"reason": reason}
            assert bool({"reason": reason}) is True

    def test_reason_codes_carry_no_domain_content(self):
        for reason in ERROR_REASONS:
            lowered = reason.lower()
            for token in ("gcal", "confirm", "evt", "standup"):
                assert token not in lowered

    def test_gate_violation_clears_result_and_installs_the_envelope(self):
        result = self.node(self._leaky_state(event_id="", event_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["result"] == ""
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_gate_violation_clears_every_output_bearing_field(self):
        result = self.node(self._leaky_state(event_id="", event_ref=""))
        for field in _OUTPUT_BEARING_FIELDS:
            assert field in result, f"{field} not written on the error return"
            assert result[field] == "", f"{field} not cleared"

    def test_framework_projection_selects_the_envelope_not_the_result(self):
        """The defect, expressed as the framework expresses it."""
        result = self.node(self._leaky_state(event_id="", event_ref=""))
        projected = result.get("formatted_output") or result.get("result")
        assert projected == {"reason": _REASON_OUTPUT_WITHHELD}
        assert "Created calendar event" not in str(projected)

    def test_error_branch_clears_result(self):
        """Direct invocation of the incoming-error branch (the pipeline
        short-circuits before execute(), so this is the direct-call path)."""
        result = self.node.execute(self._leaky_state(status=AgentStatus.ERROR.value))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["result"] == ""
        assert "Created calendar event" not in str(result)
        assert result["formatted_output"], "error-branch output must be truthy"

    def test_violation_message_names_the_path_and_never_the_value(self):
        """Quoting matched content into error_log would make the framework's
        credential scan raise on this very result - replacing it with a
        traceback and DISCARDING the clearing above it. Probed with a value
        both gates recognise."""
        secret = "Bearer " + "b" * 30
        result = self.node(_state(calendar_payload=to_json({"description": secret})))
        joined = "\n".join(result["error_log"])
        assert "calendar_payload.description" in joined
        assert secret not in joined
        assert "Bearer" not in joined
        # The clearing survived: the framework gate did not raise over it.
        assert result["result"] == ""
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}


class TestSafePath:
    """Field paths are normalised before they are interpolated into a message."""

    def test_ordinary_path_is_unchanged(self):
        assert _safe_path("calendar_payload.description") == "calendar_payload.description"
        assert _safe_path("items[0].note") == "items[0].note"

    def test_non_inert_characters_are_replaced(self):
        assert _safe_path("a b/c") == "a_b_c"
        assert " " not in _safe_path("Bearer aaaaaaaaaaaaaaaa")

    def test_path_is_length_capped(self):
        assert len(_safe_path("k" * 500)) == 120


# Every non-success path this node has. `via` says how the path is reached:
# node(state) runs the framework pipeline; execute() is used for an errored
# state because AgentBaseGraph.route() sends ERROR straight to finalize and
# BaseNode.__call__ short-circuits on it, so that branch is only reachable
# from inside.
_ERROR_PATHS = [
    pytest.param(
        {"status": AgentStatus.ERROR.value, "event_id": "", "event_ref": ""},
        "execute",
        id="inner-workflow-error",
    ),
    pytest.param(
        {
            "status": AgentStatus.ERROR.value,
            "result": {
                "event_id": "evt123",
                "event_ref": "gcal://calendars/primary/events/evt123",
                "confirmation": "Created calendar event 'Standup'",
            },
        },
        "execute",
        id="inner-workflow-error-with-answer-in-result",
    ),
    pytest.param(
        {
            "status": AgentStatus.ERROR.value,
            "error_log": [_sentinel(), "CallCalendarApiNode: rejected " + _bearer_like()],
        },
        "execute",
        id="inner-workflow-error-with-credential-in-error-log",
    ),
    pytest.param({"event_id": "", "event_ref": ""}, "call", id="gate-missing-event-evidence"),
    pytest.param(
        {"calendar_payload": to_json({"summary": "Standup", "description": _bearer_like()})},
        "call",
        id="gate-credential-nested-in-payload",
    ),
    pytest.param(
        {"calendar_payload": to_json({"summary": "Standup", "eyJ" + "c" * 40: "x"})},
        "call",
        id="gate-credential-shaped-mapping-key",
    ),
]


class TestErrorEnvelopeIsClosedSet:
    """Whatever the non-success path, the caller-visible envelope is made of
    this module's own constants - never of node-authored text.

    error_log is seeded with a recognisable sentinel on every path: a name and
    a credential-shaped token inside an echoed Google API response body, which
    is exactly what a calendar-call failure can put there. Truncating or
    credential-redacting such a line is not a closed set, so it must appear
    nowhere in what the node returns - not in a value, not in a key, at any
    depth.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _drive(self, overrides: dict, via: str) -> dict:
        state = _state(**{"error_log": [_sentinel()], **overrides})
        return self.node.execute(state) if via == "execute" else self.node(state)

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_envelope_values_are_declared_constants(self, overrides, via):
        result = self._drive(overrides, via)

        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert set(envelope) == {"reason"}, envelope
        assert set(envelope.values()) <= ERROR_REASONS, envelope

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_envelope_stays_truthy_and_the_answer_is_cleared(self, overrides, via):
        """`formatted_output or result`: a falsy envelope re-opens the fallback,
        and a surviving `result` is what it would fall back onto."""
        result = self._drive(overrides, via)

        assert result["formatted_output"], "a falsy formatted_output re-opens the `result` fallback"
        cleared = {field: result[field] for field in _OUTPUT_BEARING_FIELDS}
        assert not any(cleared.values()), cleared

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_seeded_error_text_appears_nowhere_in_the_returned_mapping(self, overrides, via):
        result = self._drive(overrides, via)

        leaves = list(_leaves(result))
        for fragment in _SENTINEL_FRAGMENTS:
            assert not any(fragment in leaf for leaf in leaves), (fragment, result)
        rendered = json.dumps(result, default=str)
        assert not any(fragment in rendered for fragment in _SENTINEL_FRAGMENTS), rendered

    def test_the_reason_names_the_path_taken(self):
        errored = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))
        withheld = self.node(_state(event_id="", event_ref="", error_log=[_sentinel()]))

        assert errored["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert withheld["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_inner_error_entries_are_not_re_emitted(self):
        """The state reducer appends error_log; re-emitting the inner entries
        would duplicate every line, and the caller never sees them anyway."""
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))

        assert "error_log" not in result

    def test_gate_violations_travel_in_error_log_only(self):
        result = self.node(_state(event_id="", event_ref="", error_log=[_sentinel()]))

        assert any("output gate" in entry for entry in result["error_log"])
        assert "output gate" not in json.dumps(result["formatted_output"])

    def test_a_caller_event_title_lands_in_a_value_and_never_in_the_label(self):
        """A caller's event title becomes the `summary` VALUE of the assembled
        body, so the violation label is built from fixed keys and indices and
        cannot carry caller text - and the caller receives the reason code
        only, so it could not reach them through the envelope anyway."""
        title = "A. Tanaka quarterly sync <a.tanaka@" + "example.com>"
        payload = {"summary": title, "description": _bearer_like()}
        result = self.node(_state(calendar_payload=to_json(payload), error_log=[_sentinel()]))

        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        label = " ".join(result["error_log"])
        assert "formatted_output['calendar_payload.description']" in label
        assert "Tanaka" not in label
        assert _bearer_like() not in label

    def test_a_credential_shaped_mapping_key_is_withheld_from_the_label(self):
        """A key that is itself credential-shaped is refused AND kept out of
        the label: quoted, it would travel in error_log, where the framework
        credential scan raises on the node result and replaces the cleared
        result - restoring the leak the clearing just closed. The clearing
        surviving is the proof that nothing raised."""
        credential_shaped_key = "eyJ" + "c" * 40
        payload = {"summary": "Standup", credential_shaped_key: "x"}
        result = self.node(_state(calendar_payload=to_json(payload)))

        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert result["result"] == ""
        assert credential_shaped_key not in json.dumps(result, default=str)
        assert any(_UNNAMEABLE_KEY in entry for entry in result["error_log"])
