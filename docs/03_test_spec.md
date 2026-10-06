# Test Specification - CMN-C2-235 Google Workspace Calendar Agent

## Test Strategy
- Test types: Unit (per node + service + config + config-arrival + inner graph) /
  Proof-of-Boundary (full outer-graph invoke, real ASGI endpoint, import
  isolation, state safety, server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/` (`tests/integration/` is an
  empty package; end-to-end backbone coverage lives in PB-6 and PB-8, which drive
  the real compiled outer graph and the real ASGI `/invoke` entry).
- The Google Calendar call is exercised through the deterministic, network-free v1
  stub transport (default) and through monkeypatched fake clients; no live Google
  Calendar call.
- **Trust-gate routing canon**: every per-node unit test invokes the node as
  `node(state)` - `BaseNode.__call__` routes the full security pipeline (trust
  gate -> input gate -> `execute()` -> output gate) - never bare
  `node.execute(state)`. State builders set `caller_trust_level =
  TrustLevel.VERIFIED_EXTERNAL.value` for PreProcessNode (the single external gate)
  and `TrustLevel.ANONYMOUS.value` for every other node. Two documented
  exceptions: `CallCalendarApiNode.execute(state, config=...)` config-override
  tests (a 2nd argument `__call__` cannot forward), and the template-owned
  refusal tests in `test_pre_process_node.py`, which call `execute()` DIRECTLY
  on purpose - the injection/PII screens must refuse hostile content with no
  framework wrapper in front (the template owns that guarantee itself). The
  trust rejection test asserts on the RETURNED error dict (`status ==
  AgentStatus.ERROR.value`, "trust gate denied" in `error_log`, execute-only
  keys absent) - `__call__` never raises for a trust denial.
- Assertion contract (wheel `agenticstar-agentcore==1.0.1`): the invoke surface is
  `result["output"]` / `status` / `trace_id` / `correlation_id` / `node_history`
  plus this template's structured envelope keys (never `formatted_output` at the
  invoke surface); status is compared to `AgentStatus.SUCCESS`/`.value` (lowercase
  `success`/`error`); the outer graph is called as `invoke(user_input=..., ctx=...)`;
  identifiers carried in request TEXT may be masked (`[MASKED]`) so text-channel
  evidence is asserted by presence, not raw repr - structured caller data
  travels the unmasked context bridge and IS asserted byte-exact; audit spies
  assert on `call.args[1]` (the event payload), never the whole-call repr.
- Real-SDK pipeline behaviors encoded by the suite: `__call__` short-circuits on an
  incoming errored state (execute() is skipped; error status/error_log pass through);
  the framework input mask rewrites Title-Case bigrams (across newlines - "Meeting
  Room" counts), emails, and digit groups in `user_input`/`validated_input` to
  `[MASKED]` before `execute()` sees the text - positive text-channel payloads are
  PII-free (single-word quoted titles, lower-case field values), intentional-PII
  tests assert the `[MASKED]` path, and the bridge regression tests assert the
  structured channel is NOT masked; injection assertions are behavioural (error
  status, nothing carried forward), never a gate's wording.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations | denial RETURNS error dict (`status == AgentStatus.ERROR.value`, "trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL passes; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | caller-data contract: serialize NL request + hint into `validated_input` (JSON); HTML strip; hint priority + bounded event-id shape; structured `event` field bounds (types, lengths, ISO datetimes, unsupported-field refusal); contact-identifier refusal with the documented name-heuristic exclusion; template-owned injection screen (control tokens, override phrasing, hostile field names, nested + escaped payloads, URL-encoded forms) via DIRECT execute(); benign-domain accept probes both directions | hint resolved by priority; malformed values -> field-named `status=error`, value never echoed; `<script>` stripped; hostile forms refused with nothing carried forward; ordinary scheduling text passes; audit payload carries presence signals only |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`); state-hint precedence over the embedded (maskable) copy | email -> `[MASKED]` before execute; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string); empty/short -> error; audit payload carries flags only |
| U-04 | test_classify_intent_node.py | intent = create_event / update_event / cancel_event (keyword; cancel first, update before create so "reschedule" never matches create); no-signal is a HARD error (write agent - never guess a write) | correct intent per keyword; no-signal -> `status=error` ("could not determine"); empty -> error; audit emits intent label only |
| U-05 | test_infer_calendar_fields_node.py | quoted-title + `Key: value` extraction; explicit ISO-like datetimes only (dateTime / all-day date shapes; non-ISO never invented); event-id resolution (text > id-shaped hint; never invented); caller-event precedence (validated structured fields authoritative, text fills gaps, JSON-string state form accepted); Google Calendar API v3 event body per intent (JSON string) | create/update body with summary/start/end/location/description; caller fields win byte-exact; cancel `{event_id}`; unresolved id left `""`; nothing extractable -> error; empty input -> error |
| U-06 | test_call_calendar_api_node.py | create/update/cancel via the network-free v1 stub; `calendar_config` state field + `execute(state, config=...)` override (documented direct-execute exception); API error / transport failure / unresolved id / unknown intent / missing payload; secret posture (live transport refuses to run unauthenticated; token read via `ctx.secrets`, never env/state) | event_id/event_ref on success; **a Google API failure logs the HTTP status ONLY** - the API's own error body (which echoes request context and can carry an organizer's name and address) appears in no log line - and a transport failure logs the exception CLASS name only, with no URL or host; live+no-secret -> error "unauthenticated"; live+bound secret -> token passed to client; audit emits presence signals with `stub_transport=True` |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; ref/id formatting; summary fallback | "Created/Updated/Cancelled calendar event ... ref=... id=..."; missing evidence -> error; audit emits reference presence only |
| U-08 | test_post_process_node.py | `formatted_output` shaping (JSON payload round-trip); errored state passes through `__call__` un-masked (short-circuit); event-evidence gate; NESTED credential scan (full-node leak probe + direct-function probes at several depths + mapping KEYS + top-level control + clean-structure pass); **error-return containment** - every non-success return clears the output-bearing state fields and installs a TRUTHY envelope, and the `formatted_output or result` projection is asserted directly; **closed-set error envelope** - parameterised over every non-success path (inner-workflow error, the same with the answer in `result`, the same with a credential in `error_log`, missing event evidence, credential nested in the payload, credential-shaped mapping key), with `error_log` seeded with a sentinel (a name + a credential-shaped token inside an echoed API body); violation messages assert the field path with the matched value absent, and the credential-shaped key withheld from the label; field-path normalisation to an inert alphabet | success shape with parsed `calendar_payload`, no reason code; error status/error_log preserved, no success shape fabricated; SUCCESS without event_id/event_ref blocked; nested credential-shaped value or key blocked naming the field PATH, value never echoed; on every non-success return the output-bearing fields are `""`, `formatted_output` is exactly `{"reason": <one of ERROR_REASONS>}` (non-empty - a falsy envelope would re-select `result`), the inner `error_log` entries are NOT re-emitted, and no fragment of the sentinel appears anywhere in the returned mapping - key or value, at any depth |
| U-09 | test_calendar_client.py | Google Calendar API v3 client: insert/patch/delete events; `Authorization: Bearer` + `Content-Type` headers; calendar_id in URL; `CalendarApiError` on non-2xx; stub shapes (Events resource echo, cancel receipt, `_stub` marker); `uses_stub_transport` | correct URLs/headers/bodies; 409 raises with the API `error.message`; stub deterministic shapes; empty live-DELETE body falls back to the cancel receipt |
| U-10 | test_config.py | flat `config/agent.yaml` manifest + `config/config.yaml` runtime file sanity | id CMN-C2-235, namespace cmn, Cat 2, CMN, ToolCallingAgent, dotted `class` entry point at root (no `agent:` nesting), VERIFIED_EXTERNAL, `generation_mode: deterministic`, `requires.secrets == []` / `extras == []`, google_calendar.base_url/calendar_id + max_retry/timeout_s in the runtime file |
| U-11 | test_config_arrival.py | declared runtime config ARRIVES at its consumer: `_parent_config()` reads the live `config/config.yaml`; inner state carries the section; full invoke constructs the client with the declared base_url/calendar_id; `max_retry`/`timeout_s` reach the graph via the server's config load; missing file degrades safely | forwarded values equal the file's values at every hop; captured client kwargs match the declaration; absent file -> `{"configurable": {}}`, no crash |
| U-12 | test_domain_workflow_graph.py | inner `CalendarWorkflowGraph`: identity, `_extra_initial_state()` calendar_config JSON injection + caller-event bridge seeding, `route()` error short-circuit, `get_output` contract, compile, direct inner invoke on the stub | name/state_schema correct; config + bridged caller data forwarded as JSON strings; error -> END; inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with event evidence |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: 0 platform-internal imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external-trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, CalendarWorkflowGraphNode, PostProcessNode, FinalizeNode]` and event evidence + confirmation + `intent=create_event` on the structured envelope; ANONYMOUS caller denied at pre_process (error, no post_process, no evidence); blank input -> error, not crash |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived - non-HITL** (no graph class declares `propagate_hitl=True`; no cross-boundary interrupt() checkpoint): module-level skipif; the stub body is a real AssertionError so enabling HITL without implementing PB-7 fails loudly |
| PB-8 | Real ASGI `/invoke` end-to-end | test_pb_invoke_endpoint.py | Bearer auth (missing/wrong token -> 401 generic body); runtime config reaches the compiled graph; authenticated create returns non-empty event evidence computed from the request; structured caller title crosses the graph boundary INTACT (bridge regression - the text channel masks it); event-hint cancel through the full stack; malformed `input_context` refused fail-closed, value never echoed; contact identifier refused; non-object context 422; >256KB context 413; injection forms refused with nothing written; benign look-alike text unaffected; no credential-shaped string anywhere in the nested response; no grouped-digit artifact, hyphenated-numeric event id round-trips byte-identical; **output-gate containment** - with the inner-to-outer merge drifted so the event evidence is lost while the inner answer still lands in `result`, the ERROR envelope carries no released text, no event reference, no traceback and no source paths, `output` is the truthy closed-set envelope `{"reason": "output_withheld_by_gate"}`, and no structured event key is surfaced - measured against a clean-path control on the same request that DOES deliver the answer |
| PB (error contract) | Caller-visible ERROR envelope | test_error_envelope_closed_set.py | **The error channel is bounded, not just the answer cleared.** Two data-path faults through the real ASGI `/invoke`: (a) the inner-to-outer merge loses the event evidence AND carries an upstream failure line in `error_log` (the reachable refusal path - the gate fires in post_process); (b) the Google Calendar client raises the documented API error with a response body carrying the sentinel (`route()` sends an ERROR status straight to `finalize`, so the invoke body is produced by `get_output()` alone). On (a) the body's `output` is exactly `{"reason": "output_withheld_by_gate"}`, truthy, with no `error_log` key, no gate wording and no traceback; on (b) `output` is empty and no event evidence is surfaced. In both, no fragment of the sentinel - a name and a credential-shaped token inside an echoed API body - appears anywhere in the response, walking every key and value at any depth. The source half is asserted at the node that owns the call: the API response body never entered `error_log` at all. Measured against a clean-path control on the same request that DOES deliver the answer |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); agent constructs + compiles via the supported path; `/invoke` + `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy
> tests (pre_process / validate / classify / infer / call / confirm nodes assert
> on the event payload, `call.args[1]`). PB-3 (live external service) is
> exercised at deployment first-invoke, not in this suite - the v1 transport is
> the documented network-free stub.

## Test Execution Summary
- Execution date: 2026-08-31
- Runner: pytest under the real wheel `agenticstar-agentcore` (verified green on
  both `1.0.1` and `1.0.2`)
- Total tests: 195
- Pass: 194 / Fail: 0 / Skip: 1 (PB-7 - auto-waived, non-HITL)
