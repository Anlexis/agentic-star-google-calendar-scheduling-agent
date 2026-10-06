"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner calendar workflow
graph has run. GraphNode.merge_output() maps the inner result into the outer
state; this node shapes the caller-facing `formatted_output`.

The domain output gate is the MODULE-LEVEL `_security_gate_output()` below,
called from execute(). It is deliberately NOT an instance method and NOT the
framework `_extra_security_gate_output` hook - the framework gate methods
are @final on FunctionNode and the real SDK auto-wraps `_extra_` hooks (which
breaks the .invoke() chain), so domain checks live in a module-level helper
invoked inline. The gate walks the WHOLE nested output structure (dicts,
lists, tuples), scanning mapping KEYS as well as values - the caller-facing
output carries the assembled Google Calendar event body as a nested mapping,
and a credential-shaped string riding one level down (e.g. inside the event
description, or as a key) must be caught exactly like a top-level one.

Containment contract: returning ERROR is NOT by itself containment. The
framework's final projection is `formatted_output or result`, with no status
check, and the outer merge step has already written the inner workflow answer
to `result` - so an error return that leaves state untouched still ships the
un-gated answer inside the ERROR envelope. Every non-success return in this
module therefore goes through the one `_contain()` helper, which CLEARS the
output-bearing state fields and installs a TRUTHY replacement: an empty string
or dict is falsy and would re-activate the `or result` fallback it is meant to
close.

What that envelope may say: CLOSED-SET LABELS ONLY. It carries a constant
reason code chosen by this module (one of `ERROR_REASONS`) and nothing else -
never `error_log`, never the gate's violation entries, never any other
node-authored string. Those lines can embed an upstream Google Calendar error
body, identifiers, titles or caller-derived fragments, and truncating or
credential-redacting them is not a closed set. `error_log` stays the INTERNAL
channel: the state reducer appends to it and the audit trail needs it; it is
simply never projected to the caller. Gate violations are written to
`error_log` naming the offending PATH (never the value), and the audit event
carries a count.

No error envelope carries calendar-write evidence either. `event_id` /
`event_ref` are this agent's WRITE EVIDENCE - the SUCCESS branch of the gate
below REFUSES an output that lacks them - so returning them under an ERROR
status would tell a caller being informed of failure that a calendar write
nonetheless occurred, and which record it touched.
"""

import re
from typing import Any, Iterator

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

# Credential-shaped strings that must never reach the caller (defence in depth -
# the framework output-side credential scan in FunctionNode also runs on every
# result).
_CREDENTIAL_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9._-]{10,}|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{16,}")

# Reason codes - the ONLY values the caller-visible ERROR envelope may carry.
# Chosen here, never derived from state, so the envelope is a closed set: it
# says WHAT happened, never to which event and never in whose words.
_REASON_WORKFLOW_FAILED = "calendar_workflow_failed"  # the inner workflow reported an error
_REASON_OUTPUT_WITHHELD = "output_withheld_by_gate"  # the output gate refused the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})

# Output-bearing outer-state fields, cleared on every non-success return so no
# un-gated content survives in state for the final projection, a checkpoint,
# or a downstream reader. `result` is the one the framework falls back to;
# the rest carry caller/domain content in their own right.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "event_summary",
    "confirmation",
    "calendar_payload",
    "redaction_flags",
    # Calendar-write evidence. Cleared on error alongside the content fields so
    # the identifiers cannot be picked up from state by a checkpoint or a
    # downstream reader after the caller has been told the operation failed.
    "event_id",
    "event_ref",
)

# Violation messages name a FIELD PATH only. Paths are built from mapping keys,
# which are code-assembled here (the event body is built from a fixed field
# set) - but a path is still interpolated into `error_log`, which the framework
# credential scan reads, so it is normalised to an inert alphabet before use.
# Quoting matched CONTENT into that message would make the framework gate raise
# on this very result and discard the clearing above it.
_PATH_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.[]")
_PATH_MAX_LEN = 120

# Stand-in for a mapping key that cannot itself be written into a path label.
# Spelled with inert characters ONLY: `_safe_path()` below rewrites anything
# outside `_PATH_SAFE_CHARS`, so the fleet's angle-bracketed `<withheld>` would
# arrive in the log as `_withheld_` and stop being a recognisable marker.
_UNNAMEABLE_KEY = "__withheld__"


def _iter_strings(value: Any, path: str) -> "Iterator[tuple[str, str]]":
    """Yield (path, string) for EVERY string in a nested structure.

    Walks dicts, lists and tuples so a value nested inside the event body
    (e.g. formatted_output["calendar_payload"]["description"]) is scanned
    exactly like a top-level field. Mapping KEYS are yielded as strings in
    their own right: `calendar_payload` is parsed back from a JSON string, so
    a credential-shaped key one level down is as publishable as a value.

    A credential-shaped key is reported as a violation by the caller of this
    walk, so the path label must not repeat it: the label travels in
    `error_log`, where the framework's own credential scan would raise on it
    and replace the cleared result with a bare error - restoring the very leak
    the clearing closed.
    """
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                label = _UNNAMEABLE_KEY if _CREDENTIAL_LIKE_RE.search(key) else key
                key_path = f"{path}.{label}" if path else label
                yield key_path, key
            else:
                key_path = f"{path}.{_UNNAMEABLE_KEY}" if path else _UNNAMEABLE_KEY
            yield from _iter_strings(item, key_path)
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            yield from _iter_strings(item, f"{path}[{idx}]")


def _safe_path(path: str) -> str:
    """Normalise a field path to an inert alphabet before it enters a message.

    The message names the LOCATION of a finding and never its content; this
    keeps that true even if a mapping key were ever caller-influenced. Any
    character outside the inert set becomes "_", and the path is length-capped.
    """
    trimmed = path[:_PATH_MAX_LEN]
    return "".join(char if char in _PATH_SAFE_CHARS else "_" for char in trimmed)


def _security_gate_output(formatted_output: "dict[str, Any]", is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Blocks (returns violations for):
      - a SUCCESS response with no event evidence (event_id/event_ref), which
        would misrepresent the calendar write outcome to the caller;
      - any credential-shaped string ANYWHERE in the caller-facing output -
        nested mappings, list entries and mapping keys included.

    A violation names the field PATH, never the value. Violations are
    `error_log` entries (internal); they never reach the caller.
    """
    problems: "list[str]" = []
    if is_success and not (formatted_output.get("event_id") or formatted_output.get("event_ref")):
        problems.append("PostProcess output gate: SUCCESS output missing event_id/event_ref evidence")
    for path, value in _iter_strings(formatted_output, ""):
        if _CREDENTIAL_LIKE_RE.search(value):
            problems.append(f"PostProcess output gate: credential-like value in formatted_output['{_safe_path(path)}']")
    return problems


def _cleared_output_state() -> "dict[str, Any]":
    """Every output-bearing state field, blanked.

    Part of EVERY non-success return so the framework's
    `formatted_output or result` projection cannot fall back onto the
    un-gated inner answer (see the containment contract in the module
    docstring). Values are empty strings: msgpack-safe and falsy, so a
    downstream `or` on any of them behaves as "absent".
    """
    return {field: "" for field in _OUTPUT_BEARING_FIELDS}


def _contain(reason: str, new_errors: "list[str] | None" = None) -> "dict[str, Any]":
    """The node result for ANY non-success outcome - the single error shape.

    Error status, every output-bearing field cleared (`_cleared_output_state`),
    and an envelope made of closed-set labels only: `reason` is one of
    `ERROR_REASONS`. `new_errors` (gate violations - path labels only) are
    written to `error_log`, the internal channel the state reducer accumulates,
    and never enter the envelope. Nothing is read out of state: not the event,
    not `error_log`.

    The constant `reason` key keeps the mapping TRUTHY, so the framework's
    `formatted_output or result` projection (AgentBaseGraph.get_output()
    applies no status check) serves this envelope and never whatever survived
    in `result`.
    """
    contained: "dict[str, Any]" = _cleared_output_state()
    contained["formatted_output"] = {"reason": reason}
    contained["status"] = AgentStatus.ERROR.value
    if new_errors:
        contained["error_log"] = list(new_errors)
    return contained


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # Read-only formatting of the already-produced result - default permissive;
    # the external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        # If the inner workflow errored, preserve the error status (do not mask
        # it) and publish NOTHING of it. Under the real pipeline this branch is
        # unreachable twice over - AgentBaseGraph.route() sends an ERROR status
        # straight to `finalize`, and BaseNode.__call__ short-circuits on an
        # already-errored state before execute() runs - so it is the
        # direct-invocation path, and it must still be a closed set: the moment
        # routing changes (a HITL resume routes to post_process regardless of
        # status) it becomes the caller's envelope.
        #
        # error_log is NOT re-emitted: the state reducer appends to it, so the
        # inner entries are already there and re-emitting would duplicate every
        # line. The caller receives the reason code only.
        if state.get("status") == AgentStatus.ERROR.value:
            # Outcome signals only - a closed-set reason code and a count. The
            # audit log is not a store for event content or error text.
            emit_trace_event(
                "post_process_error_contained",
                {"reason": _REASON_WORKFLOW_FAILED, "errors": len(state.get("error_log", []))},
                state,
            )
            return _contain(_REASON_WORKFLOW_FAILED)

        formatted_output = {
            "event_id": state.get("event_id", ""),
            "event_ref": state.get("event_ref", ""),
            "event_summary": state.get("event_summary", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
            "calendar_payload": from_json(state.get("calendar_payload"), {}),
        }

        # Domain output gate (module-level helper - see module docstring). A
        # refusal is contained the same way as an inner error: the assembled
        # output is DISCARDED, every output-bearing state field is cleared so
        # the answer already merged into `result` cannot be projected into the
        # ERROR envelope, the violations go to `error_log` only, and the caller
        # receives the reason code alone.
        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            emit_trace_event(
                "post_process_gate_blocked",
                {"reason": _REASON_OUTPUT_WITHHELD, "violations": len(violations)},
                state,
            )
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_event_id": bool(state.get("event_id")),
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
